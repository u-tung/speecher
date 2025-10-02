import asyncio as aio
import logging
import os
import re
import tempfile
import time
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Iterable, Mapping
from datetime import timedelta
from threading import Thread
from typing import Any, Generic, NamedTuple, Protocol, Self, TypeVar

from simple_audio import Player

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)


_T = TypeVar("_T", bound=type)


class ErrorPolicy:
    Error: type["Error"]
    Ignore: type["Ignore"]
    Retry: type["Retry"]

    @classmethod
    def register(cls, obj: _T) -> _T:
        setattr(cls, obj.__name__, obj)
        return obj


@ErrorPolicy.register
class Error(ErrorPolicy): ...


@ErrorPolicy.register
class Ignore(ErrorPolicy): ...


@ErrorPolicy.register
class Retry(ErrorPolicy):
    def __init__(self, count: int):
        self.count = count


class PlayerLike(Protocol):
    @abstractmethod
    def play(self) -> None: ...
    @abstractmethod
    def pause(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...

    @property
    def volume(self) -> float: ...
    @volume.setter
    def volume(self, value: float) -> None: ...


_T = TypeVar("_T")


class BaseSpeecher:
    SUBPROCESS_TIMEOUT = timedelta(seconds=15)

    _speaking: bool
    rate: int | None
    voice: str | None
    quality: int | None
    _errorPolicy: ErrorPolicy
    subprocessTimeout: timedelta

    def __init__(
        self,
        rate: int | None = None,
        voice: str | None = None,
        quality: int | None = None,
        *,
        errorPolicy: ErrorPolicy = ErrorPolicy.Error(),
        subprocessTimeout: timedelta | float = SUBPROCESS_TIMEOUT,
    ):
        self._speaking = False
        self.rate = rate
        self.voice = voice
        self.quality = quality

        self._errorPolicy = errorPolicy
        if isinstance(subprocessTimeout, timedelta):
            self.subprocessTimeout = subprocessTimeout
        else:
            self.subprocessTimeout = timedelta(seconds=subprocessTimeout)

    @property
    def errorPolicy(self) -> ErrorPolicy:
        return self._errorPolicy

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def paused(self) -> bool:
        return not self._speaking

    @property
    def status(self) -> dict:
        return {
            "speaking": self.speaking,
            "paused": self.paused,
            "rate": self.rate,
            "voice": self.voice,
            "quality": self.quality,
        }

    @property
    def voiceList(self) -> list[str]:
        with os.popen("say --voice '?'") as response:
            return response.readlines()

    @property
    def commandParams(self) -> list[str]:
        kwargs = {"--rate": self.rate, "--voice": self.voice, "--quality": self.quality}
        args = []
        for key, value in kwargs.items():
            if value is not None:
                args.append(key)
                args.append(str(value))

        return args

    @abstractmethod
    def speak(self, string: str) -> Any: ...

    async def _timeout_retry(
        self,
        handle: Callable[..., Coroutine[Any, Any, _T]],
        *,
        count: int,
        args: Iterable[Any] = (),
        kwargs: Mapping[str, Any] = {},
    ) -> _T:
        error = ValueError("count must be greater then or equal to 0")
        for i in range(count + 1):
            try:
                return await handle(*args, **kwargs)
            except TimeoutError as err:
                if i != count:
                    logger.warning("subprocess timeout, retry")
                error = err
        logger.error("subprocess repeat timeout, error: %r", error)
        raise error


class PureSpeecher(BaseSpeecher):

    async def speak(self, string: str) -> int:
        """
        return say command's return code.
        when the subprocess timeout and the errorPolicy is Ignore, return 143
        """
        count = 0
        if isinstance(self.errorPolicy, ErrorPolicy.Retry):
            count = self.errorPolicy.count

        try:
            return await self._timeout_retry(self._speak, args=[string], count=count)
        except TimeoutError:
            if isinstance(self.errorPolicy, ErrorPolicy.Ignore):
                logger.warning("subprocess timeout, ignore")
                return 143
            raise

    async def _speak(self, string: str) -> int:
        self._speaking = True
        proc = await aio.create_subprocess_exec(
            "say", *self.commandParams, "--", string
        )
        try:
            return await aio.wait_for(proc.wait(), self.subprocessTimeout.seconds)
        except TimeoutError:
            proc.terminate()
            raise
        finally:
            self._speaking = False


class SequentialSpeecher(BaseSpeecher):
    """
    The difference between SequentialSpeecher and PureSpeecher is that
    when SequentialSpeecher.speak is called multiple times at the same time,
    SequentialSpeecher will speak all the strings passed to SequentialSpeecher.speak in order.
    """

    _processPipe: "PreprocessPipe[dict, dict]"

    def __init__(
        self,
        rate: int | None = None,
        voice: str | None = None,
        quality: int | None = None,
        *,
        errorPolicy: ErrorPolicy = ErrorPolicy.Error(),
        subprocessTimeout: timedelta | float = BaseSpeecher.SUBPROCESS_TIMEOUT,
    ):
        super().__init__(
            rate,
            voice,
            quality,
            errorPolicy=errorPolicy,
            subprocessTimeout=subprocessTimeout,
        )
        self._processPipe = PreprocessPipe(
            maxNumOfPreprocess=2,
            processQueueMaxSize=8,
        )

        @self._processPipe.registerPreprocess
        async def _(raw: dict) -> dict:
            return raw

        @self._processPipe.registerProcess
        async def _(item: dict) -> None:
            count = 0
            if isinstance(self.errorPolicy, ErrorPolicy.Retry):
                count = self.errorPolicy.count

            try:
                return await self._timeout_retry(
                    self._speak,
                    kwargs={
                        k: v
                        for k, v in item.items()
                        if k in ["onSpeaking", "onFinish", "string"]
                    },
                    count=count,
                )
            except TimeoutError as err:
                if isinstance(self.errorPolicy, ErrorPolicy.Ignore):
                    logger.warning("subprocess timeout, ignore")
                    return
                logger.error("subprocess timeout, error: %r", err)
                raise

    async def _speak(
        self, string: str, onSpeaking: Callable, onFinish: Callable
    ) -> None:
        self._speaking = True
        proc = await aio.create_subprocess_exec(
            "say", *self.commandParams, "--", string
        )

        onSpeaking()
        try:
            await aio.wait_for(proc.wait(), self.subprocessTimeout.seconds)
        except TimeoutError:
            proc.terminate()
            raise
        finally:
            self._speaking = False
        onFinish(string)

    def speak(
        self,
        string: str,
        onSpeaking: Callable = lambda *_, **__: None,
        onFinish: Callable = lambda *_, **__: None,
        *,
        split: bool = False,
    ) -> None:
        phases = [string]
        if split:
            phases = self._split_phase(string)

        for phase in phases:
            self._processPipe.push(
                {"string": phase, "onSpeaking": onSpeaking, "onFinish": onFinish}
            )

    @staticmethod
    def _split_phase(string: str) -> list[str]:
        return [*filter(lambda x: x, re.split(r"(?:，|。|：|\n)+", string))]

    async def waitDone(self) -> None:
        await self._processPipe.waitDone()

    def close(self) -> None:
        self._processPipe.close()

    def __del__(self) -> None:
        self.close()


class BufferedSpeecher(SequentialSpeecher, PlayerLike):
    _processPipe: "PreprocessPipe[dict, dict]"
    _player: Player | None
    _volume: float
    _playableState: aio.Event

    class SpeakingEvent(NamedTuple):
        text: str
        duration: float

    def __init__(
        self,
        rate: int | None = None,
        voice: str | None = None,
        quality: int | None = None,
        *,
        maxNumOfSynthesizer: int = 2,
        errorPolicy: ErrorPolicy = ErrorPolicy.Error(),
        subprocessTimeout: timedelta | float = BaseSpeecher.SUBPROCESS_TIMEOUT,
    ):
        BaseSpeecher.__init__(
            self,
            rate,
            voice,
            quality,
            errorPolicy=errorPolicy,
            subprocessTimeout=subprocessTimeout,
        )
        self._processPipe = PreprocessPipe(
            maxNumOfPreprocess=maxNumOfSynthesizer,
            processQueueMaxSize=8,
        )
        self._playableState = aio.Event()
        self._playableState.set()
        self.volume = 1
        self._player = None

        @self._processPipe.registerPreprocess
        async def _(raw: dict) -> dict:
            count = 0
            if isinstance(self.errorPolicy, ErrorPolicy.Retry):
                count = self.errorPolicy.count

            outFile = tempfile.NamedTemporaryFile("rb", suffix=".wav")
            try:
                await self._timeout_retry(
                    self.synthesizeVoice, args=[raw["string"], outFile], count=count
                )
                return raw | {"outFile": outFile}

            except Exception as err:
                outFile.close()
                match err, self.errorPolicy:
                    case TimeoutError(), ErrorPolicy.Ignore():
                        logger.warning("synthesizing voice timeout, ignore")
                        return raw | {"outFile": None}
                logger.error("%r", err)
                raise

            except aio.CancelledError:
                outFile.close()
                raise

        @self._processPipe.registerProcess
        async def _(item: dict) -> None:
            if item["outFile"] is None:
                return

            await self._playableState.wait()
            logger.debug("play audio file: %s", item["outFile"].name)
            self._speaking = True
            try:
                self._player = Player(item["outFile"]).play()
                # relieve the event loop freezing for too long
                await aio.sleep(0)

                event = self.SpeakingEvent(item["string"], self._player.duration)
                item["onSpeaking"](event)

                await self._player.await_done()
                logger.debug("play done file: %s", item["outFile"].name)
                self._speaking = False
                item["onFinish"](event)

            except aio.CancelledError:
                self.stop()
                raise

            finally:

                def destruct(player: Player | None, file: tempfile._TemporaryFileWrapper):
                    # delay a little bit to stagger the construction of the subsequent
                    # Player, which prevents the lock acquired by player.close
                    # from blocking it.
                    time.sleep(1)
                    if player is not None:
                        player.close()
                    file.close()

                Thread(
                    target=destruct,
                    args=(self._player, item["outFile"]),
                    daemon=True,
                ).start()
                self._player = None

    async def synthesizeVoice(
        self, string: str, outFile: tempfile._TemporaryFileWrapper
    ) -> None:
        args = (
            "say",
            *self.commandParams,
            "--file-format=WAVE",
            "--data-format=F32@88200",
            "-o",
            outFile.name,
            "--",
            string,
        )
        proc = await aio.create_subprocess_exec(*args)

        logger.debug("wait subprocess: %s(args: %s)", proc, args)
        try:
            await aio.wait_for(proc.wait(), self.subprocessTimeout.seconds)
        except TimeoutError:
            proc.terminate()
            raise
        logger.debug("done subprocess: %s", proc)

    def speak(
        self,
        string: str,
        onSpeaking: Callable[[SpeakingEvent], None] = lambda _: None,
        onFinish: Callable[[SpeakingEvent], None] = lambda _: None,
        *,
        split: bool = False,
    ) -> None:
        return super().speak(string, onSpeaking, onFinish, split=split)

    def stop(self) -> None:
        if self._player is not None:
            self._player.close()
        self.close()

    def pause(self) -> None:
        self._playableState.clear()

    def play(self) -> None:
        self._playableState.set()

    def close(self) -> None:
        if self._player is not None:
            self._player.close()
        super().close()

    @property
    def paused(self) -> bool:
        return not self._playableState.is_set()

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        assert 0 <= value <= 1
        self._volume = value


_RawType = TypeVar("_RawType")
_CookedType = TypeVar("_CookedType")
_PreprocessType = Callable[[_RawType], Coroutine[Any, Any, _CookedType]]
_ProcessType = Callable[[_CookedType], Coroutine[Any, Any, None]]


class PreprocessPipe(Generic[_RawType, _CookedType]):
    preprocess: _PreprocessType[_RawType, _CookedType]
    process: _ProcessType[_CookedType]
    _processing: bool
    _processor: aio.Task[None]
    _preprocessor: aio.Task[None]
    _preprocessQueue: aio.Queue[_RawType]
    _processQueue: aio.Queue[aio.Future[_CookedType]]
    _currProcessTask: aio.Task[None] | None
    _maxNumOfPreprocess: int
    _processQueueMaxSize: int

    def __init__(
        self,
        preprocess: _PreprocessType[_RawType, _CookedType] | None = None,
        process: _ProcessType[_CookedType] | None = None,
        *,
        maxNumOfPreprocess,
        processQueueMaxSize,
    ):
        if preprocess is not None:
            self.preprocess = preprocess
        if process is not None:
            self.process = process
        self._maxNumOfPreprocess = maxNumOfPreprocess
        self._processQueueMaxSize = processQueueMaxSize

        self._processing = False
        self._processQueue = aio.Queue(self._processQueueMaxSize)
        self._preprocessQueue = aio.Queue()

        self._preprocessor = aio.create_task(self._preprocessorMain())
        self._processor = aio.create_task(self._processorMain())
        self._currProcessTask = None

    def registerProcess(self, func: _ProcessType[_CookedType]) -> None:
        self.process = func

    def registerPreprocess(self, func: _PreprocessType[_RawType, _CookedType]) -> None:
        self.preprocess = func

    @property
    def processing(self) -> bool:
        return self._processing

    @property
    def maxNumOfPreprocess(self) -> int:
        return self._maxNumOfPreprocess

    def push(self, item: _RawType) -> Self:
        self._preprocessQueue.put_nowait(item)
        return self

    async def waitDone(self) -> None:
        await self._preprocessQueue.join()
        await self._processQueue.join()

    async def _processorMain(self) -> None:
        try:
            while True:
                cooked = await (await self._processQueue.get())

                self._processing = True
                self._currProcessTask = aio.create_task(self.process(cooked))
                await self._currProcessTask
                self._processing = False

                self._processQueue.task_done()

        except aio.CancelledError:
            return

    async def _preprocessorMain(self) -> None:
        semaphore = aio.Semaphore(self.maxNumOfPreprocess)
        task_refs = set()

        async def dispatch_preprocess(raw):
            future = aio.Future()
            await self._processQueue.put(future)

            async def wrapper():
                future.set_result(await self.preprocess(raw))
                semaphore.release()
                task_refs.remove(task)

            task = aio.create_task(wrapper())
            task_refs.add(task)

        try:
            while True:
                await semaphore.acquire()
                raw = await self._preprocessQueue.get()
                await dispatch_preprocess(raw)
                self._preprocessQueue.task_done()

        except aio.CancelledError:
            return

    def close(self) -> None:
        self._preprocessor.cancel()
        self._processor.cancel()
        if self._currProcessTask is not None:
            self._currProcessTask.cancel()

    def __del__(self):
        self.close()
