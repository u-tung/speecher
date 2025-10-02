# speecher
An wrapper of macOS say command

# Example
```python
imoprt time
from speecher import BufferedSpeecher, ErrorPolicy

try:
  speecher = BufferedSpeecher(rate=225, errorPolicy=ErrorPolicy.Retry(2))
  speecher.speak("Hello, World!", onFinish=lambda: print("Done!"))
  time.sleep(3)
finally:
  speecher.close()
```
