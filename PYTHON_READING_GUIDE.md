# 面向本项目的 Python 读码入门

这份教程不是一本完整的 Python 教材，而是为了帮你读懂这个仓库里的代码。

如果你今天只有几个小时，建议先不要从这份长 Markdown 开始硬读。请优先打开同目录下的 `python_interactive_tutorial.html`，它已经改成了 4 小时训练营：31 个小关卡，每关只讲一个基础点，先用纯 Python 小例子练，再在最后回到 USB6363 项目读码。

如果你已经做完训练营，想补齐这份 Markdown 里剩下的工程化读码知识，请打开同目录下的 `python_reading_supplement.html`。它覆盖正则、`**kwargs`、bytes、HTTP 细节、`staticmethod`、枚举、`argparse`、锁、内部类、继承、错误链和项目调用链复盘。这份 Markdown 更适合当作后续查漏补缺的参考书。

目标是掌握少量高频知识，能够看懂大约 80% 的代码。先不用追求会写复杂程序，也不用把所有语法背下来。读代码时最重要的是先看懂：

1. 程序从哪里开始运行。
2. 数据以什么形式传来传去。
3. 函数和类分别负责什么。
4. 出错时会发生什么。
5. 这个项目里几个文件之间如何协作。

## 0. 先认识这个项目

这个仓库主要有 5 个 Python 文件：

```text
minimal_dev2_check.py      最小硬件检查脚本
example_child_program.py   子程序调用 API 的例子
usb6363_client.py          给其他 Python 程序用的客户端
usb6363_server.py          本地 HTTP API 服务
usb6363_core.py            真正访问 USB-6363 硬件的底层代码
```

可以把它们想成一条调用链：

```text
example_child_program.py
    -> Usb6363Client
    -> HTTP 请求
    -> usb6363_server.py
    -> DaqController
    -> nidaqmx
    -> USB-6363 硬件
```

最重要的设计思想是：

```text
只有 usb6363_core.py 直接碰硬件。
其他程序通过 usb6363_client.py 访问本地服务。
```

这样可以避免多个 Python 程序同时抢同一块采集卡。

## 1. Python 文件是怎么运行的

看 `minimal_dev2_check.py` 和 `example_child_program.py`，末尾都有这样的写法：

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

这是 Python 脚本常见入口。

含义是：

```text
如果这个文件是被 python xxx.py 直接运行的，就执行 main()。
如果这个文件只是被别的文件 import，就不要自动执行 main()。
```

所以你读一个 Python 脚本时，可以先找：

```python
def main() -> int:
    ...

if __name__ == "__main__":
    raise SystemExit(main())
```

然后从 `main()` 开始读。

例如 `example_child_program.py` 的阅读顺序大概是：

```text
1. from usb6363_client import Usb6363Client
2. def main()
3. daq = Usb6363Client()
4. daq.get_device()
5. daq.read_ai(...)
6. daq.read_pfi(...)
7. daq.count_pfi_edges(...)
8. daq.write_ao(...)
```

## 2. import 是什么

项目里有很多 `import`：

```python
import json
from typing import Any
from usb6363_client import Usb6363Client
from usb6363_core import DaqController, DEVICE_NAME
```

你可以把 `import` 理解为“拿来别的模块里的工具”。

常见形式有两种：

```python
import json
```

表示导入整个 `json` 模块。使用时写：

```python
json.dumps(data)
json.loads(raw)
```

另一种：

```python
from usb6363_core import DaqController
```

表示只从 `usb6363_core.py` 里拿出 `DaqController` 这个名字。使用时直接写：

```python
controller = DaqController()
```

本项目里最关键的 import 关系是：

```text
minimal_dev2_check.py  -> import DaqController
example_child_program.py -> import Usb6363Client
usb6363_server.py -> import DaqController
usb6363_client.py -> import urllib/json 等标准库
usb6363_core.py -> import nidaqmx
```

特别注意：只有 `usb6363_core.py` import 了 `nidaqmx`。这就是项目隔离硬件访问的关键。

## 3. 注释和文档字符串

Python 里有两类很常见的说明文字。

第一类是普通注释：

```python
# 创建客户端。默认连接 http://127.0.0.1:8765。
daq = Usb6363Client()
```

`#` 后面的内容不会执行，只是给人看的。

第二类是文档字符串，也叫 docstring：

```python
def health(self) -> dict[str, Any]:
    """检查服务是否正在运行。"""

    return self._get("/health")
```

函数、类、文件开头的三引号字符串通常用来说明它的用途。读陌生代码时，docstring 很值得先看。

## 4. 变量和基本类型

变量就是给一个值起名字。

```python
DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_PORT = 8765
timeout = 10.0
value = False
```

本项目里常见类型有这些：

### 字符串 str

```python
channel = "ai0"
line = "PFI0"
base_url = "http://127.0.0.1:8765"
```

字符串就是文本。通常用引号包起来。

### 整数 int

```python
samples = 1
DEFAULT_PORT = 8765
```

整数没有小数点。

### 浮点数 float

```python
rate = 1000.0
timeout = 10.0
value = 1.23
```

浮点数就是带小数的数字。电压、采样率、等待秒数常用 float。

### 布尔值 bool

```python
value = True
value = False
```

布尔值只有两个：`True` 和 `False`。注意首字母大写。

项目里 `read_pfi()` 返回的数字电平就是布尔值：

```python
pfi_result = daq.read_pfi(line="PFI0")
print(pfi_result["value"])
```

### None

```python
params: dict[str, Any] | None = None
```

`None` 表示“没有值”。有点像空。比如 `_get()` 里的 `params` 默认是 `None`，表示没有 URL 参数。

### list 列表

```python
device_names = [device.name for device in system.devices]
```

列表是一串值，用方括号。

简单例子：

```python
channels = ["ai0", "ai1", "ai2"]
```

读取列表里的某一项：

```python
channels[0]   # "ai0"
channels[1]   # "ai1"
```

Python 从 0 开始计数。

### dict 字典

字典是一组“键 -> 值”的数据。

```python
return {
    "device": self.device_name,
    "channel": physical_channel,
    "samples": samples,
    "rate": rate,
    "values": values,
}
```

可以理解成一张小表：

```text
device  -> Dev2
channel -> Dev2/ai0
values  -> 1.234
```

读取字典：

```python
result["channel"]
result["values"]
```

示例代码里：

```python
ai_result = daq.read_ai(channel="ai0")
print(f"ai0 = {ai_result['values']:.6f} V")
```

这里 `ai_result` 是字典，`ai_result["values"]` 取出电压值。

## 5. f-string：把变量塞进字符串

项目里常见这种写法：

```python
print(f"ai0 = {ai_result['values']:.6f} V")
```

前面的 `f` 表示这是 f-string。大括号 `{}` 里面可以放变量或表达式。

例子：

```python
name = "Dev2"
print(f"Using {name}")
```

输出：

```text
Using Dev2
```

格式控制也常见：

```python
{ai_result['values']:.6f}
```

意思是把数字显示成小数点后 6 位。

```python
{ao_result['value']:.3f}
```

意思是显示小数点后 3 位。

## 6. 函数 def

函数是“把一段逻辑起个名字”。

例如 `usb6363_client.py` 里：

```python
def health(self) -> dict[str, Any]:
    """检查服务是否正在运行。"""

    return self._get("/health")
```

结构是：

```python
def 函数名(参数):
    函数体
    return 返回值
```

再看一个更有代表性的：

```python
def read_ai(
    self,
    channel: str = "ai0",
    samples: int = 1,
    rate: float = 1000.0,
    terminal_config: str = "RSE",
    min_val: float = -10.0,
    max_val: float = 10.0,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return self._get(
        "/api/ai/read",
        {
            "channel": channel,
            "samples": samples,
            "rate": rate,
            "terminal_config": terminal_config,
            "min_val": min_val,
            "max_val": max_val,
            "timeout": timeout,
        },
    )
```

你现在先掌握 4 件事：

1. `read_ai` 是函数名。
2. 括号里是参数。
3. `channel: str = "ai0"` 表示参数默认值是 `"ai0"`，类型提示是字符串。
4. `-> dict[str, Any]` 表示这个函数预计返回一个字典。

调用时可以这样：

```python
daq.read_ai()
```

这会使用所有默认参数。

也可以这样：

```python
daq.read_ai(channel="ai1", samples=100, rate=1000)
```

这会覆盖其中几个默认参数。

## 7. 参数、默认值、关键字参数

这个项目大量使用默认参数：

```python
def write_ao(
    self,
    channel: str = "ao0",
    value: float = 0.0,
    min_val: float = -10.0,
    max_val: float = 10.0,
    timeout: float = 10.0,
) -> dict[str, Any]:
```

这意味着你可以只传一部分：

```python
daq.write_ao(value=1.23)
```

等价于：

```python
daq.write_ao(
    channel="ao0",
    value=1.23,
    min_val=-10.0,
    max_val=10.0,
    timeout=10.0,
)
```

像 `channel="ao0"` 这种写法叫关键字参数。优点是清楚，不容易把顺序写错。

## 8. 类型提示先不用怕

项目里有很多这样的写法：

```python
def main() -> int:
```

```python
def _read_json(self) -> dict[str, Any]:
```

```python
def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
```

这些是类型提示。它们主要给人和编辑器看，不是 Python 运行所必须的。

常见写法含义：

```text
name: str              name 应该是字符串
samples: int           samples 应该是整数
rate: float            rate 应该是小数
value: bool            value 应该是 True/False
dict[str, Any]         字典，键是字符串，值可以是各种类型
list[DeviceInfo]       列表，里面每一项是 DeviceInfo
float | list[float]    要么是一个 float，要么是一串 float
... | None             也可以是 None
```

例如：

```python
values: float | list[float] = value
```

意思是 `values` 可能是一个数字，也可能是数字列表。

为什么？因为读 1 个采样点时返回一个数，读多个采样点时返回列表。

## 9. class 类和 self

本项目有两个最重要的类：

```python
class DaqController:
```

和：

```python
class Usb6363Client:
```

可以先把类理解成“一组相关函数和数据的打包”。

### Usb6363Client

`Usb6363Client` 是给其他程序用的客户端。

```python
daq = Usb6363Client()
```

这行代码创建了一个客户端对象。

然后可以调用它的方法：

```python
daq.get_device()
daq.read_ai(channel="ai0")
daq.write_ao(channel="ao0", value=0.0)
```

方法就是放在类里面的函数。

### self 是什么

类里面的方法通常第一个参数是 `self`：

```python
def health(self) -> dict[str, Any]:
    return self._get("/health")
```

`self` 表示“当前这个对象自己”。

比如：

```python
daq = Usb6363Client()
```

当你调用：

```python
daq.health()
```

Python 实际上会把 `daq` 作为 `self` 传进去。

所以你不用手动写：

```python
daq.health(daq)
```

只写：

```python
daq.health()
```

### __init__ 是初始化函数

`usb6363_client.py` 里：

```python
def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0) -> None:
    self.base_url = base_url.rstrip("/")
    self.timeout = timeout
```

`__init__` 会在创建对象时自动执行。

所以：

```python
daq = Usb6363Client()
```

会自动设置：

```python
daq.base_url = "http://127.0.0.1:8765"
daq.timeout = 10.0
```

如果你写：

```python
daq = Usb6363Client(base_url="http://127.0.0.1:9000")
```

那 `daq.base_url` 就会变成 9000 端口。

## 10. 对象属性

对象里的变量叫属性。

```python
self.base_url = base_url.rstrip("/")
self.timeout = timeout
self.device_name = device_name
self._lock = threading.RLock()
```

属性可以被同一个对象的其他方法使用。

例如 `Usb6363Client._get()` 里：

```python
url = f"{self.base_url}{path}"
```

这里用的就是前面 `__init__` 保存的 `self.base_url`。

## 11. 下划线开头的方法

你会看到很多这样的名字：

```python
_get
_post
_decode_response
_normalize_channel
_terminal_config
```

Python 里单下划线开头通常表示：“这是内部辅助函数，普通使用者不需要直接调用”。

例如你平时应该调用：

```python
daq.read_ai(channel="ai0")
```

而不是调用：

```python
daq._get(...)
```

`_get()` 是 `read_ai()` 内部帮你发 HTTP 请求用的。

## 12. if / elif / else 条件判断

条件判断是读代码最重要的基础之一。

`usb6363_server.py` 里有典型例子：

```python
if parsed.path == "/health":
    self._send_json({"ok": True, "device": controller.device_name})
elif parsed.path == "/api/devices":
    devices = [device.__dict__ for device in controller.list_devices()]
    self._send_json({"devices": devices})
elif parsed.path == "/api/device":
    self._send_json(controller.get_device_info().__dict__)
else:
    self._send_error(HTTPStatus.NOT_FOUND, "Unknown route")
```

意思是：

```text
如果路径是 /health，就返回健康检查。
否则如果路径是 /api/devices，就返回设备列表。
否则如果路径是 /api/device，就返回当前设备信息。
否则返回 404 Unknown route。
```

`if` 后面的表达式必须是能判断真假的东西。

例如 `usb6363_core.py` 里：

```python
if samples < 1:
    raise ValueError("samples must be >= 1")
if rate <= 0:
    raise ValueError("rate must be > 0")
```

这就是参数检查：

```text
采样点数不能小于 1。
采样率必须大于 0。
```

## 13. try / except：处理错误

服务器代码里有：

```python
try:
    ...
except Exception as exc:
    self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
```

含义是：

```text
先尝试执行 try 里的代码。
如果中间出错，就跳到 except。
把错误转换成 JSON 返回给调用方。
```

客户端代码里也有：

```python
try:
    with urlopen(url, timeout=self.timeout) as response:
        return self._decode_response(response.read())
except HTTPError as exc:
    raise RuntimeError(self._error_message(exc)) from exc
```

含义是：

```text
尝试发 HTTP 请求。
如果服务器返回 HTTP 错误，就把它转换成 RuntimeError。
```

你读代码时，看到 `try` 可以先问：

```text
作者认为这里可能会失败。
失败以后他想怎么处理？
```

## 14. raise：主动报错

`raise` 表示主动抛出错误。

例如：

```python
if samples < 1:
    raise ValueError("samples must be >= 1")
```

如果传入：

```python
read_ai_voltage(samples=0)
```

程序不会继续往下执行，而是报错：

```text
ValueError: samples must be >= 1
```

这不是坏事。它是在尽早阻止错误参数进入硬件操作。

常见错误类型：

```text
ValueError      参数值不合法
RuntimeError    程序运行状态不对，比如找不到设备
HTTPError       HTTP 请求失败
```

## 15. return：函数返回结果

函数用 `return` 把结果交给调用者。

例如 `read_ai_voltage()` 最后：

```python
return {
    "device": self.device_name,
    "channel": physical_channel,
    "samples": samples,
    "rate": rate,
    "values": values,
}
```

所以调用：

```python
result = controller.read_ai_voltage(channel="ai0")
```

`result` 就会得到这个字典。

再比如：

```python
def main() -> int:
    ...
    return 0
```

通常 `return 0` 表示程序正常结束，`return 1` 表示程序失败结束。

## 16. with：自动收尾

项目里有很多 `with`：

```python
with nidaqmx.Task() as task:
    task.ai_channels.add_ai_voltage_chan(...)
    value = float(task.read(timeout=timeout))
```

还有：

```python
with self._lock:
    ...
```

`with` 的作用是“进入一段需要收尾的上下文，结束后自动清理”。

对 `nidaqmx.Task()` 来说：

```text
进入 with：创建硬件任务。
离开 with：自动释放硬件资源。
```

对 `self._lock` 来说：

```text
进入 with：拿到锁。
离开 with：释放锁。
```

这个项目用锁是为了让硬件访问排队执行，避免多个请求同时操作同一块 USB-6363。

## 17. for 循环和列表推导式

普通循环在 `minimal_dev2_check.py` 里：

```python
for device in devices:
    print(f"  {device.name}: {device.product_type}, serial={device.serial_num}")
```

意思是：对 `devices` 列表里的每个 `device`，执行一次打印。

项目里还有一种简洁写法，叫列表推导式：

```python
device_names = [device.name for device in system.devices]
```

等价于：

```python
device_names = []
for device in system.devices:
    device_names.append(device.name)
```

再看 `list_signal_terminals()` 里的例子：

```python
"pfi": [terminal for terminal in terminals if "/PFI" in terminal],
```

意思是：

```text
从 terminals 里挑出包含 "/PFI" 的项，组成一个新列表。
```

这种语法刚开始看会有点挤。读的时候可以按这个顺序拆：

```python
[结果 for 临时变量 in 原列表 if 条件]
```

## 18. 字典里的 get

代码里常见：

```python
body.get("channel", "ao0")
```

意思是：

```text
从 body 字典里取 "channel"。
如果没有这个键，就用默认值 "ao0"。
```

对比：

```python
body["value"]
```

这个写法要求 `body` 里必须有 `"value"`。如果没有，会报错。

所以在 `_ao_args()` 里：

```python
return {
    "channel": str(body.get("channel", "ao0")),
    "value": float(body["value"]),
    "min_val": float(body.get("min_val", -10.0)),
    "max_val": float(body.get("max_val", 10.0)),
    "timeout": float(body.get("timeout", 10.0)),
}
```

含义是：

```text
channel 可选，默认 ao0。
value 必须提供。
min_val/max_val/timeout 可选。
```

## 19. 类型转换

HTTP 和 JSON 传来的数据经常是字符串或通用数据，需要转换成正确类型。

项目里常见：

```python
int(...)
float(...)
str(...)
bool(...)
```

例如 `_ai_args()`：

```python
"samples": int(_first(query, "samples", 1)),
"rate": float(_first(query, "rate", 1000.0)),
```

因为 URL 查询参数本质上是字符串：

```text
/api/ai/read?samples=100&rate=1000
```

这里的 `"100"` 和 `"1000"` 要转换成数字，底层硬件函数才好用。

再看 `_bool_value()`：

```python
if isinstance(value, bool):
    return value
if isinstance(value, (int, float)):
    return bool(value)
if isinstance(value, str):
    normalized = value.strip().lower()
    if normalized in ("true", "1", "yes", "on", "high"):
        return True
    if normalized in ("false", "0", "no", "off", "low"):
        return False
raise ValueError(f"Invalid boolean value: {value!r}")
```

这段函数的作用是把很多用户可能传入的写法统一成 Python 的 `True` 或 `False`。

例如：

```text
true   -> True
"1"    -> True
"high" -> True
"0"    -> False
"low"  -> False
```

## 20. *args、**kwargs 和 **字典展开

这个项目里有一个重要写法：

```python
self._send_json(controller.read_ai_voltage(**_ai_args(query)))
```

先看 `_ai_args(query)` 返回什么：

```python
{
    "channel": "ai0",
    "samples": 1,
    "rate": 1000.0,
    "terminal_config": "RSE",
    "min_val": -10.0,
    "max_val": 10.0,
    "timeout": 10.0,
}
```

前面的 `**` 会把这个字典展开成关键字参数。

也就是说：

```python
controller.read_ai_voltage(**_ai_args(query))
```

等价于：

```python
controller.read_ai_voltage(
    channel="ai0",
    samples=1,
    rate=1000.0,
    terminal_config="RSE",
    min_val=-10.0,
    max_val=10.0,
    timeout=10.0,
)
```

这在 API 层很常见：先把 HTTP 参数整理成字典，再展开传给底层函数。

## 21. JSON 是什么

本项目用 HTTP API，所以经常出现 JSON。

JSON 是一种通用数据格式，长得很像 Python 字典：

```json
{
  "channel": "ao0",
  "value": 1.23
}
```

Python 里的字典：

```python
{
    "channel": "ao0",
    "value": 1.23,
}
```

非常像，但不是同一个东西。需要转换。

客户端发送 POST 时：

```python
payload = json.dumps(data).encode("utf-8")
```

含义是：

```text
Python dict -> JSON 字符串 -> UTF-8 bytes
```

服务器读 POST body 时：

```python
raw = self.rfile.read(length).decode("utf-8")
return json.loads(raw)
```

含义是：

```text
bytes -> 字符串 -> Python dict
```

服务器返回 JSON 时：

```python
payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
```

客户端解析响应时：

```python
data = json.loads(raw.decode("utf-8"))
```

记住一句就够：

```text
json.dumps 是 Python 变 JSON。
json.loads 是 JSON 变 Python。
```

## 22. HTTP 的 GET 和 POST

`usb6363_server.py` 里有：

```python
def do_GET(self) -> None:
```

和：

```python
def do_POST(self) -> None:
```

GET 通常用于读取或查询：

```text
GET /health
GET /api/device
GET /api/ai/read?channel=ai0
GET /api/pfi/read?line=PFI0
```

POST 通常用于会改变状态的操作：

```text
POST /api/ao/write
POST /api/pfi/write
```

这对应硬件含义：

```text
读 AI、读 PFI、查询设备信息：GET
写 AO、写 PFI：POST
```

`usb6363_client.py` 把这些 HTTP 细节封装起来了，所以你平时只需要写：

```python
daq.read_ai(channel="ai0")
daq.write_ao(channel="ao0", value=1.23)
```

## 23. 正则表达式在这里做什么

`usb6363_core.py` 开头有：

```python
_AI_RE = re.compile(r"^(?:(?P<device>Dev\d+)/)?ai(?P<index>\d+)$")
_AO_RE = re.compile(r"^(?:(?P<device>Dev\d+)/)?ao(?P<index>\d+)$")
```

这是正则表达式，用来检查通道名字是否合法。

你现在不需要精通正则，只要知道它负责判断这些字符串：

```text
ai0
Dev2/ai0
ao0
Dev2/ao0
PFI0
Dev2/PFI0
port0/line0
```

例如 `_normalize_channel()` 里：

```python
pattern = _AI_RE if kind == "ai" else _AO_RE
match = pattern.match(channel)
if match is None:
    raise ValueError(f"Invalid {kind.upper()} channel: {channel!r}")
```

意思是：

```text
根据 kind 选择 AI 或 AO 的规则。
拿规则去匹配 channel。
如果匹配不上，就报错。
```

匹配成功后：

```python
device = match.group("device") or self.device_name
index = int(match.group("index"))
```

提取设备名和通道编号。

如果用户只写 `"ai0"`，没有写设备名，代码就用默认设备：

```python
device = match.group("device") or self.device_name
```

`or` 在这里可以理解成：

```text
如果左边有值，用左边。
否则用右边。
```

## 24. dataclass 是什么

`usb6363_core.py` 里有：

```python
@dataclass(frozen=True)
class DeviceInfo:
    name: str
    product_type: str
    serial_num: str
```

`dataclass` 适合用来定义“只装数据的小对象”。

不用 dataclass 时，你可能要手写：

```python
class DeviceInfo:
    def __init__(self, name, product_type, serial_num):
        self.name = name
        self.product_type = product_type
        self.serial_num = serial_num
```

用了 dataclass，Python 自动帮你生成这些初始化代码。

所以可以直接写：

```python
DeviceInfo(
    name=device.name,
    product_type=device.product_type,
    serial_num=str(device.serial_num),
)
```

`frozen=True` 表示创建后不希望再改它的属性。

## 25. @staticmethod 是什么

`usb6363_core.py` 里：

```python
@staticmethod
def _terminal_config(name: str) -> TerminalConfiguration:
```

静态方法是不需要使用 `self` 的方法。

普通方法：

```python
def _normalize_channel(self, channel: str, kind: str) -> str:
    ...
    return f"{self.device_name}/{kind}{index}"
```

它需要 `self.device_name`，所以必须是普通方法。

静态方法：

```python
@staticmethod
def _edge(name: str) -> Edge:
    key = name.strip().upper()
    ...
```

它只处理传进来的 `name`，不需要对象自己的属性，所以可以写成 `@staticmethod`。

刚开始读代码时，你不必太纠结。看到 `@staticmethod` 就理解成：

```text
这个函数放在类里，但不依赖某个具体对象。
```

## 26. 枚举和方括号取值

`usb6363_core.py` 里：

```python
return TerminalConfiguration[key]
```

和：

```python
return Edge[key]
```

`TerminalConfiguration` 和 `Edge` 来自 `nidaqmx.constants`，它们类似一组选项。

例如：

```text
TerminalConfiguration["RSE"]
Edge["RISING"]
```

把字符串转换成库需要的枚举值。

外层代码允许用户传字符串：

```python
terminal_config="RSE"
edge="RISING"
```

底层再转换成 `nidaqmx` 接受的对象。

## 27. 字符串常用方法

本项目常见几个字符串方法：

```python
base_url.rstrip("/")
name.strip().upper()
value.strip().lower()
```

含义：

```text
rstrip("/")    去掉字符串末尾的 /
strip()        去掉字符串两边的空格、换行等
upper()        转大写
lower()        转小写
```

例如：

```python
key = name.strip().upper()
```

如果用户传入 `" rising "`，会变成 `"RISING"`。

这样用户输入稍微不规范也没关系。

## 28. bytes 和 encode/decode

HTTP 传输时经常是 bytes，不是普通字符串。

客户端：

```python
payload = json.dumps(data).encode("utf-8")
```

服务器：

```python
raw = self.rfile.read(length).decode("utf-8")
```

简单理解：

```text
encode("utf-8")   字符串 -> bytes
decode("utf-8")   bytes -> 字符串
```

你平时写业务调用时基本不会碰它，因为 `Usb6363Client` 已经封装好了。

## 29. 这个项目最重要的三个类和函数

### Usb6363Client

位置：`usb6363_client.py`

它给“未来你的其他 Python 子程序”使用。

常用方法：

```python
daq = Usb6363Client()
daq.health()
daq.get_device()
daq.read_ai(channel="ai0")
daq.write_ao(channel="ao0", value=1.23)
daq.read_pfi(line="PFI0")
daq.write_pfi(line="PFI0", value=True)
daq.count_pfi_edges(line="PFI0", counter="ctr0", seconds=1.0)
```

你未来写实验脚本，大概率只需要懂这一层。

### make_handler(controller)

位置：`usb6363_server.py`

它创建一个 HTTP 请求处理类，把 URL 路由到对应的硬件操作。

比如：

```text
/api/ai/read   -> controller.read_ai_voltage(...)
/api/ao/write  -> controller.write_ao_voltage(...)
/api/pfi/read  -> controller.read_digital_line(...)
```

这层是“网络接口层”。

### DaqController

位置：`usb6363_core.py`

它是真正调用 `nidaqmx` 的地方。

常用方法：

```python
controller.list_devices()
controller.get_device_info()
controller.read_ai_voltage(channel="ai0")
controller.write_ao_voltage(channel="ao0", value=1.23)
controller.read_digital_line(line="PFI0")
controller.write_digital_line(line="PFI0", value=True)
controller.count_pfi_edges(line="PFI0")
```

这层是“硬件控制层”。

## 30. 一次 read_ai 调用到底发生了什么

假设你在 `example_child_program.py` 里写：

```python
ai_result = daq.read_ai(channel="ai0")
```

第一步，调用 `Usb6363Client.read_ai()`。

它会把参数打包成字典：

```python
{
    "channel": "ai0",
    "samples": 1,
    "rate": 1000.0,
    "terminal_config": "RSE",
    "min_val": -10.0,
    "max_val": 10.0,
    "timeout": 10.0,
}
```

第二步，`read_ai()` 调用内部的 `_get()`：

```python
return self._get("/api/ai/read", {...})
```

第三步，`_get()` 拼出 URL：

```text
http://127.0.0.1:8765/api/ai/read?channel=ai0&samples=1&...
```

第四步，服务器的 `do_GET()` 收到请求，发现路径是：

```text
/api/ai/read
```

于是执行：

```python
self._send_json(controller.read_ai_voltage(**_ai_args(query)))
```

第五步，`_ai_args(query)` 把 URL 字符串参数转成正确类型：

```text
"1" -> 1
"1000.0" -> 1000.0
```

第六步，`DaqController.read_ai_voltage()` 真正访问硬件：

```python
with nidaqmx.Task() as task:
    task.ai_channels.add_ai_voltage_chan(...)
    value = float(task.read(timeout=timeout))
```

第七步，底层返回字典：

```python
{
    "device": "Dev2",
    "channel": "Dev2/ai0",
    "samples": 1,
    "rate": 1000.0,
    "values": 1.234,
}
```

第八步，服务器把字典变成 JSON 发回客户端。

第九步，客户端把 JSON 变回 Python 字典，赋值给：

```python
ai_result
```

所以你最后可以写：

```python
print(ai_result["values"])
```

## 31. 如何读一个你不熟悉的函数

推荐按这个顺序读：

1. 看函数名。
2. 看参数和默认值。
3. 看 docstring。
4. 看 `return` 返回什么。
5. 看里面有没有 `raise`，知道什么情况会失败。
6. 看它调用了哪些其他函数。

以 `write_ao_voltage()` 为例：

```python
def write_ao_voltage(
    self,
    channel: str = "ao0",
    value: float = 0.0,
    min_val: float = -10.0,
    max_val: float = 10.0,
    timeout: float = 10.0,
) -> dict[str, Any]:
```

先读到：

```text
它写 AO 电压。
默认通道 ao0。
默认值 0.0 V。
默认允许范围 -10 到 +10 V。
返回 dict。
```

再看参数检查：

```python
if not min_val <= value <= max_val:
    raise ValueError(f"value must be between {min_val} and {max_val}")
```

知道电压超范围会报错。

再看核心操作：

```python
with nidaqmx.Task() as task:
    task.ao_channels.add_ao_voltage_chan(...)
    task.write(float(value), auto_start=True, timeout=timeout)
```

知道这里真的写硬件。

最后看返回：

```python
return {
    "device": self.device_name,
    "channel": physical_channel,
    "value": float(value),
}
```

知道调用者会拿到设备名、通道名、实际写入值。

## 32. 如何读一个类

读类时不需要从第一行一路硬啃。按这个顺序：

1. 看类名和 docstring，知道它代表什么。
2. 看 `__init__`，知道对象创建时保存了什么状态。
3. 看不带下划线的公开方法，知道外部可以怎么用它。
4. 最后再看下划线开头的辅助方法。

以 `Usb6363Client` 为例：

```text
__init__          保存 base_url 和 timeout
health            检查服务
list_devices      列出设备
get_device        获取当前设备
list_terminals    列出端子
read_ai           读模拟输入
write_ao          写模拟输出
read_pfi          读数字电平
write_pfi         写数字电平
count_pfi_edges   计数 PFI 边沿
_get              内部 GET 请求
_post             内部 POST 请求
_decode_response  内部解析 JSON 响应
_error_message    内部提取错误信息
```

你作为使用者，优先掌握公开方法就够了。

## 33. 本项目的安全边界

这段代码操作真实硬件，所以有几个设计很重要。

### 只有 core 直接使用 nidaqmx

```python
import nidaqmx
```

只出现在 `usb6363_core.py`。

这意味着未来其他脚本尽量不要直接碰硬件库，而是通过：

```python
from usb6363_client import Usb6363Client
```

### 硬件操作加锁

`DaqController.__init__()` 里：

```python
self._lock = threading.RLock()
```

各个硬件方法里：

```python
with self._lock:
    ...
```

含义是同一时间只允许一个硬件操作进入。

### 写输出前做参数检查

例如 AO：

```python
if not min_val <= value <= max_val:
    raise ValueError(...)
```

例如通道编号：

```python
if index > max_index:
    raise ValueError(...)
```

这能减少误操作硬件的风险。

## 34. 你最需要记住的 20% Python 知识

如果只记一页，就记这些：

```text
import        从别的文件或库拿工具。
变量          name = value。
str/int/float/bool/list/dict 是最常见数据类型。
def           定义函数。
return        返回结果。
class         把相关数据和函数打包。
self          当前对象自己。
__init__      创建对象时自动运行。
if/elif/else  条件判断。
for           遍历列表。
try/except    捕获错误。
raise         主动报错。
with          自动管理资源，比如硬件任务、锁、网络响应。
dict["key"]   从字典取必需值。
dict.get(...) 从字典取可选值。
f"{x}"        把变量插进字符串。
json.dumps    Python 变 JSON。
json.loads    JSON 变 Python。
**dict        把字典展开成函数关键字参数。
_name         内部辅助函数或属性。
```

## 35. 建议你的读码路线

第一遍，不要从 `usb6363_core.py` 开始。它最底层，概念最多。

建议顺序：

1. `example_child_program.py`
2. `usb6363_client.py`
3. `usb6363_server.py`
4. `usb6363_core.py`
5. `minimal_dev2_check.py`

第一遍只问：

```text
这个文件对外提供什么能力？
它调用了哪个文件？
它返回的数据长什么样？
```

第二遍再看：

```text
参数怎么传？
错误怎么处理？
哪些地方真的操作硬件？
```

第三遍才需要研究：

```text
正则表达式怎么校验通道？
nidaqmx.Task 具体怎么配置？
HTTPServer 具体怎么工作？
```

## 36. 未来你写子程序时的最小模板

以后你大概率可以从这个模板开始：

```python
from __future__ import annotations

from usb6363_client import Usb6363Client


def main() -> int:
    daq = Usb6363Client()

    ai0 = daq.read_ai(channel="ai0")
    print(f"ai0 = {ai0['values']:.6f} V")

    pfi0 = daq.read_pfi(line="PFI0")
    print(f"PFI0 = {pfi0['value']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

运行前先启动服务：

```powershell
python usb6363_server.py
```

然后在另一个终端运行你的子程序。

## 37. 读不懂时怎么拆

遇到一行看不懂的 Python，可以按下面方式拆。

例如：

```python
self._send_json(controller.read_ai_voltage(**_ai_args(query)))
```

先从最里面看：

```python
_ai_args(query)
```

它把 HTTP 查询参数变成字典。

再看：

```python
controller.read_ai_voltage(**...)
```

它调用底层硬件读取函数。

再看：

```python
self._send_json(...)
```

它把结果发回 HTTP 客户端。

所以整行意思是：

```text
把 URL 参数整理好，读取 AI 电压，再把结果作为 JSON 返回。
```

再比如：

```python
devices = [device.__dict__ for device in controller.list_devices()]
```

拆开：

```python
controller.list_devices()
```

得到一组 `DeviceInfo` 对象。

```python
device.__dict__
```

把对象转成字典。

```python
[... for device in ...]
```

对每个设备都做一次转换，得到字典列表。

整行意思是：

```text
获取设备列表，并把每个设备对象转换成 JSON 容易发送的字典。
```

## 38. 最后：你现在不必掌握的内容

下面这些东西在项目里出现了，但初期不需要深入：

```text
http.server 的内部机制
BaseHTTPRequestHandler 的完整生命周期
正则表达式完整语法
nidaqmx 的全部通道配置
线程锁的底层实现
Python 类型系统的高级玩法
```

你只需要先知道它们在这里分别负责：

```text
HTTPServer    接收本机 API 请求。
正则          检查通道名字是否合法。
nidaqmx       操作 NI 采集卡。
锁            让硬件访问排队。
类型提示      帮助读代码和减少误用。
```

等你能顺畅读懂 `example_child_program.py` 和 `usb6363_client.py`，再往 `server` 和 `core` 深入，就会轻松很多。
