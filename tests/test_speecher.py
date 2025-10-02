from speecher import (
    PureSpeecher,
    BufferedSpeecher,
    PreprocessPipe,
    SequentialSpeecher,
    ErrorPolicy,
    logger as speecher_logger
)
import os
import unittest
import asyncio


class TestPureSpeecher(unittest.IsolatedAsyncioTestCase):

    def setUp(self, ):
        self.speecher = PureSpeecher()

    async def test_using(self, ):
        await self.speecher.speak("你好")
        self.assertEqual(self.speecher.speaking, False)
        self.assertEqual(self.speecher.paused, True)

        task = asyncio.create_task(self.speecher.speak("我叫 Siri，我可以說中文"))
        await asyncio.sleep(0)
        self.assertEqual(self.speecher.speaking, True)
        self.assertEqual(self.speecher.paused, False)
        self.assertIsInstance(self.speecher.status, dict)
        await task

    async def test_using2(self, ):
        self.speecher.rate = 150
        self.speecher.voice = "Meijia"
        self.speecher.quality = 127

        self.assertEqual(await self.speecher.speak("你好我叫美嘉"), 0)

    async def test_voiceList(self, ):
        self.assertIsInstance(self.speecher.voiceList, list)

    def test_errorPolicy_Error(self, ):
        policy = ErrorPolicy.Error()
        speecher = PureSpeecher(errorPolicy=policy, subprocessTimeout=0)
        self.assertRaises(
            TimeoutError,
            lambda: asyncio.run(speecher.speak("我應該會超時"))
        )

    async def test_errorPolicy_Ignore(self, ):
        policy = ErrorPolicy.Ignore()
        speecher = PureSpeecher(errorPolicy=policy, subprocessTimeout=0)
        self.assertEqual(await speecher.speak("我應該會超時"), 143)

    async def test_errorPolicy_Retry(self, ):
        policy = ErrorPolicy.Retry(2)
        speecher = PureSpeecher(errorPolicy=policy, subprocessTimeout=0)

        count = 0
        async def mock(*_, **__):
            nonlocal count
            if count == 2:
                return 0
            count += 1
            raise TimeoutError
        speecher._speak = mock # type: ignore

        self.assertEqual(await speecher.speak("我應該會超時"), 0)
        self.assertEqual(count, 2)


class TestSequentialSpeecher(unittest.IsolatedAsyncioTestCase):

    async def test_using(self, ):
        speecher = SequentialSpeecher()
        for c in "你好，我是 Siri":
            speecher.speak(c)
        await speecher.waitDone()

    async def test_errorPolicy_Retry(self, ):
        policy = ErrorPolicy.Retry(2)
        speecher = SequentialSpeecher(subprocessTimeout=0.01, errorPolicy=policy)

        count = 0
        async def mock(*_, **__):
            nonlocal count
            if count == 2:
                return 0
            count += 1
            raise TimeoutError
        speecher._speak = mock # type: ignore

        self.assertEqual(speecher.speak("我應該會超時"), None)
        await speecher.waitDone()
        self.assertEqual(count, 2)


class TestBufferedSpeecher(unittest.IsolatedAsyncioTestCase):

    async def test_using(self, ):
        speecher = BufferedSpeecher()
        # speecher._processPipe._preprocessQueue
        for c in "你好，我是 Siri":
            if c.strip():
                speecher.speak(c)
        await speecher.waitDone()

    async def test_large_text(self, ):
        text = \
            """
            本手册仅描述 Python 编程语言，不宜当作教程。
            我希望尽可能地保证内容精确无误，但还是选择使用自然词句进行描述，正式的规格定义仅用于句法和词法解析。这样应该能使文档对于普通人来说更易理解，但也可能导致一些歧义。
            因此，如果你是来自火星并且想凭借这份文档把 Python 重新实现一遍，也许有时需要自行猜测，实际上最终大概会得到一个十分不同的语言。
            而在另一方面，如果你正在使用 Python 并且想了解有关该语言特定领域的精确规则，你应该能够在这里找到它们。如果你希望查看对该语言更正式的定义，也许你可以花些时间自己写上一份 --- 或者发明一台克隆机器 :-)
            在语言参考文档里加入过多的实现细节是很危险的 --- 具体实现可能发生改变，对同一语言的其他实现可能使用不同的方式。而在另一方面，CPython 是得到广泛使用的 Python 实现 (然而其他一些实现的拥护者也在增加)，其中的特殊细节有时也值得一提，特别是当其实现方式导致额外的限制时。因此，你会发现在正文里不时会跳出来一些简短的 "实现注释"。
            每种 Python 实现都带有一些内置和标准的模块。相关的文档可参见 Python 标准库 索引。少数内置模块也会在此提及，如果它们同语言描述存在明显的关联。
            1.1. 其他实现
            虽然官方 Python 实现差不多得到最广泛的欢迎，但也有一些其他实现对特定领域的用户来说更具吸引力。
            知名的实现包括:
            CPython
            这是最早出现并持续维护的 Python 实现，以 C 语言编写。新的语言特性通常在此率先添加。
            Jython
            以 Java 语言编写的 Python 实现。 此实现可以作为 Java 应用的一个脚本语言，或者可以用来创建需要 Java 类库支持的应用。 想了解更多信息请访问 Jython 网站。
            Python for .NET
            此实现实际上使用了 CPython 实现，但是属于 .NET 托管应用并且可以引入 .NET 类库。它的创造者是 Brian Lloyd。想了解详情可访问 Python for .NET 主页。
            IronPython
            另一个 .NET 版 Python 实现，不同于 Python.NET，这是一个生成 IL 的完整 Python 实现，并会将 Python 代码直接编译为 .NET 程序集。 它的创造者就是当初创造 Jython 的 Jim Hugunin。 想了解更多信息，请参看 IronPython 网站。
            PyPy
            一个完全用 Python 语言编写的 Python 实现。 它支持多个其他实现所没有的高级特性，例如非栈式支持和即时编译器等。 此项目的目标之一是通过允许方便地修改解释器（因为它是用 Python 编写的）来鼓励对语言本身的试验。 更多信息可在 PyPy 项目主页 获取。
            以上这些实现都可能在某些方面与此参考文档手册的描述有所差异，或是引入了超出标准 Python 文档范围的特定信息。请参考它们各自的专门文档，以确定你正在使用的这个实现有哪些你需要了解的东西。
            """
        speecher = BufferedSpeecher()
        for line in text.splitlines():
            line = line.strip()
            if line:
                speecher.speak(line)
        await speecher.waitDone()

    async def test_volume(self, ):
        speecher = BufferedSpeecher()
        for i in range(1, 10, 2):
            speecher.volume = 0.1*i
            speecher.speak(f"音量 {i}0%")
            await speecher.waitDone()

    async def test_errorPolicy_Ignore(self, ):
        policy = ErrorPolicy.Ignore()
        speecher = BufferedSpeecher(subprocessTimeout=0.01, errorPolicy=policy)
        speecher.speak("我應該會超時")
        self.assertEqual(await speecher.waitDone(), None)

    async def test_errorPolicy_Retry(self, ):
        policy = ErrorPolicy.Retry(2)
        speecher = BufferedSpeecher(subprocessTimeout=0.01, errorPolicy=policy)

        count = 0
        async def mock(string, outFile, **__):
            nonlocal count
            if count == 2:
                os.system(f"say --file-format=AIFF -o {outFile.name} ''")
                return
            count += 1
            raise TimeoutError
        speecher.synthesizeVoice = mock # type: ignore

        speecher.speak("我應該會超時")
        self.assertEqual(await speecher.waitDone(), None)
        self.assertEqual(count, 2)

    async def test_dash_string_bug(self, ):
        speecher = BufferedSpeecher()
        speecher.speak("--error_parameter")
        self.assertEqual(await speecher.waitDone(), None)
        self.assertEqual(speecher._processPipe._processor.done(), False)


class TestPreprocessPipe(unittest.IsolatedAsyncioTestCase):

    async def test_using(self, ):
        pipe: PreprocessPipe[int | float, int | float]
        pipe = PreprocessPipe()

        preprocessTimes = 0
        processRecord = []

        async def preprocess(item: float | int):
            nonlocal preprocessTimes
            await asyncio.sleep(item)
            preprocessTimes += 1
            return item
        pipe.preprocess = preprocess

        @pipe.registerProcess
        async def _(item: int | float):
            self.assertEqual(pipe.processing, True)
            processRecord.append(item)

        for i in range(3, -1, -1):
            pipe.push(0.1*i)

        await asyncio.sleep(0.3)

        await pipe.waitDone()
        self.assertEqual(preprocessTimes, 4)
        self.assertEqual(
            [round(x, 1) for x in processRecord],
            [0.3, 0.2, 0.1, 0.0]
        )

    @unittest.skip("Not Implemented")
    async def test_error1(self, ):
        pipe: PreprocessPipe[int, int]
        pipe = PreprocessPipe()
        self.assertRaises(..., pipe.push(1)) # type: ignore

    @unittest.skip("Not Implemented")
    async def test_error2(self, ):
        pipe: PreprocessPipe[int, int]
        pipe = PreprocessPipe()
        @pipe.registerPreprocess
        async def _(item: int):
            return item

        pipe.push(1)
        self.assertRaises(..., await asyncio.sleep(0.01)) # type: ignore