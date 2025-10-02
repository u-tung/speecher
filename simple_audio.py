import asyncio
import atexit
import functools
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import singledispatchmethod
from io import BufferedReader
from pathlib import Path
from threading import Lock, RLock
from typing import Literal, NamedTuple, Self, cast, overload

import numpy as np
import pyaudio
from soundfile import SoundFile
from typing_extensions import Buffer

port_audio = pyaudio.PyAudio()
port_audio_lock = Lock()


@atexit.register
def _():
    port_audio.terminate()


@dataclass
class Device:
    index: int
    structVersion: int
    hostApi: str
    name: str
    maxInputChannels: int
    maxOutputChannels: int
    defaultLowInputLatency: int
    defaultLowOutputLatency: int
    defaultHighInputLatency: int
    defaultHighOutputLatency: int
    defaultSampleRate: int


class StreamOption(NamedTuple):
    port_audio_format: int
    """
    port_audio_format:
        paFloat32 = pa.paFloat32  #: 32 bit float
        paInt32 = pa.paInt32  #: 32 bit int
        paInt24 = pa.paInt24  #: 24 bit int
        paInt16 = pa.paInt16  #: 16 bit int
        paInt8 = pa.paInt8  #: 8 bit int
        paUInt8 = pa.paUInt8  #: 8 bit unsigned int
        paCustomFormat = pa.paCustomFormat  #: a custom data format
    """
    buffer_type: Literal["float64", "float32", "int32", "int16"]
    sample_width: Literal[2, 4, 8]


def list_devices() -> list[Device]:
    devices = []
    for i in range(port_audio.get_device_count()):
        info = port_audio.get_device_info_by_index(i)
        devices.append(Device(**info))  # pyright: ignore[reportArgumentType]
    return devices


def get_default_output_device() -> Device:
    return Device(**port_audio.get_default_output_device_info()) # pyright: ignore[reportArgumentType]


_CallbackReturnCode = int  # pyaudio.paContinue | pyaudio.paComplete | pyaudio.paAbort


class Player:
    output_device: Device | None
    frames_per_buffer: int
    _stream: pyaudio.Stream | None
    _reader: SoundFile
    _playing_completion_event: asyncio.Event
    _lock: RLock

    __stream_callback_is_done: bool
    __async_loop: asyncio.AbstractEventLoop | None

    ################################################################################
    # Class Variables
    ################################################################################

    _playing_player_refs: set["Player"] = set()
    DEFAULT_FRAMES_PER_BUFFER = 4096

    ################################################################################
    # Static Methods
    ################################################################################

    @staticmethod
    def _lock_method(method):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)

        return wrapper

    ################################################################################
    # Dunder Methods
    ################################################################################

    @overload
    def __init__(
        self, reader: SoundFile, *, frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER
    ): ...

    @overload
    def __init__(
        self,
        filePath: str | Path,
        *,
        frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER,
    ): ...

    @overload
    def __init__(
        self,
        file: BufferedReader,
        *,
        frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER,
    ): ...

    def __init__(
        self, *args, frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER, **kwargs
    ):
        self.__init_(*args, **kwargs)
        self.output_device = None
        self.frames_per_buffer = frames_per_buffer

        self._stream = None
        self._playing_completion_event = asyncio.Event()

        self._lock = RLock()

        self.__stream_callback_is_done = False
        self.__async_loop = None

    @singledispatchmethod
    def __init_(self, file: BufferedReader):
        self._reader = SoundFile(file, "rb")

    @__init_.register
    def _(self, filepath: str | Path):
        self._reader = SoundFile(filepath)

    @__init_.register
    def _(self, reader: SoundFile):
        self._reader = reader

    @_lock_method
    def __del__(self):
        self.close()

    ################################################################################
    # Methods
    ################################################################################

    @_lock_method
    def play(self) -> Self:
        assert self._stream is None or self.paused and self._stream is not None

        if self.playing:
            assert self._stream is not None
            assert self in self._playing_player_refs
            raise RuntimeError("play the player that is currently playing")

        self._update_playing_status()

        if self.paused and self._stream is not None:
            self._stream.start_stream()
            return self

        try:
            self.__async_loop = asyncio.get_running_loop()
        except RuntimeError:
            self.__async_loop = None

        device_index = (
            self.output_device.index if self.output_device is not None else None
        )
        with port_audio_lock:
            self._stream = port_audio.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                output_device_index=device_index,
                stream_callback=self._stream_callback,  # pyright: ignore[reportArgumentType]
                frames_per_buffer=self.frames_per_buffer,
            )

        return self

    async def await_done(self) -> Self:
        await self._playing_completion_event.wait()
        return self

    def wait_done(self) -> Self:
        if self._stream is not None:
            while self._stream.is_active():
                time.sleep(0.05)
        return self

    @_lock_method
    def pause(self) -> Self:
        if self.playing:
            assert self._stream is not None
            self._update_non_playing_status()
            self._stream.stop_stream()
        return self

    @_lock_method
    def close(self) -> None:
        self._playing_completion_event.set()
        if not self._reader.closed:
            self._reader.close()

        try:
            if self.playing:
                self._update_non_playing_status()
            if self._stream is not None:
                with port_audio_lock:
                    self._stream.close()
        except (OSError, AttributeError):
            # prevent throwing errors when port_audio has been terminated.
            pass

        self._stream = None

    ################################################################################
    # Properties
    ################################################################################

    @property
    def channels(self) -> int:
        return self._reader.channels

    @property
    def sample_rate(self) -> int:
        return self._reader.samplerate

    @property
    def playing(self) -> bool:
        if self._stream is None:
            return False
        return self._stream.is_active()

    @property
    def paused(self) -> bool:
        if self._stream is None:
            return False
        return self._stream.is_stopped()

    @property
    def duration(self) -> float:
        return self._reader.frames / self._reader.samplerate

    @property
    def current_time(self) -> float:
        return self._reader.tell() / self._reader.samplerate

    @current_time.setter
    @_lock_method
    def current_time(self, time: float):
        assert 0 <= time <= self.duration
        self._reader.seek(int(time * self._reader.samplerate))

    ################################################################################
    # Internal Methods
    ################################################################################

    @_lock_method
    def _update_playing_status(self) -> None:
        self.__stream_callback_is_done = False
        self._playing_completion_event.clear()
        self._playing_player_refs.add(self)

    @_lock_method
    def _update_non_playing_status(self) -> None:
        self._playing_player_refs.remove(self)

    @_lock_method
    def _complete_playing(self) -> None:
        self._playing_completion_event.set()
        self._update_non_playing_status()

    @_lock_method
    def _stream_callback(
        self,
        in_data: bytes | None,
        frame_count: int,
        time_info: Mapping[str, float],
        status: int,
    ) -> tuple[Buffer, _CallbackReturnCode]:
        # sometimes _stream_callback is called weirdly after returning paComplete
        if self.__stream_callback_is_done:
            return b"", pyaudio.paComplete

        buf = self._reader.read(frame_count, "float32")
        data = np.frombuffer(buf, np.uint8)

        if data.shape[0] == 0:
            self.__stream_callback_is_done = True
            if self.__async_loop:
                self.__async_loop.call_soon_threadsafe(self._complete_playing)
            else:
                self._complete_playing()
            return b"", pyaudio.paComplete

        frame_size = 4 * self._reader.channels
        data_frames = len(data) // frame_size

        if data_frames < frame_count:
            data = np.pad(data, (0, frame_count * frame_size - len(data)))

        return cast(Buffer, data), pyaudio.paContinue
