# BLE HID AT UART 工具

基于 Windows WinRT 的 BLE 设备通信工具，支持通过系统已配对的蓝牙设备进行 GATT 通信。
专为 BLE HID 设备设计，可直接连接已配对但扫描不可见的设备。

## 功能特性

- ✅ **直接连接系统配对设备** - 无需扫描，直接使用 MAC 地址连接已配对设备
- ✅ **BLE HID 设备支持** - 可连接已连但扫描不可见的 HID 设备
- ✅ **GATT 服务枚举** - 自动发现并列出所有服务和特征
- ✅ **双向通信** - 支持写入和通知特征的数据收发（Hex/文本）
- ✅ **状态管理** - 完整的连接状态机（DISCONNECTED/CONNECTING/CONNECTED/ERROR）
- ✅ **事件回调** - 支持连接、断开、状态变化、数据接收等事件监听
- ✅ **自动重连** - 内置重连功能，支持断线重连
- ✅ **同步 API** - 所有方法都是同步阻塞的，易于使用
- ✅ **命令行交互** - main.py 内置交互式测试模式

## 技术栈

- **Python 3.8+**
- **winsdk** - Windows SDK Python 绑定，用于访问 WinRT API
- **asyncio + threading** - 后台异步事件循环，对外透明

## 安装依赖

```bash
pip install -r requirements.txt
```

主要依赖：
```
winsdk>=1.0.0
```

## 快速开始

### 1. 基本用法

```python
from main import BLEDevice

# 创建并连接设备（替换为你的设备 MAC 地址）
ble = BLEDevice("4E:2F:74:8C:F5:19")
if not ble.connect():
    print("连接失败")
    exit()

print(f"设备名称: {ble.name}")
print(f"当前状态: {ble.state.value}")

# 枚举服务和特征
for svc in ble.list_services():
    print(f"Service: {svc['uuid']}")
    for char in svc['characteristics']:
        print(f"  {char['uuid']} [{', '.join(char['properties'])}]")

# 选择写入和通知特征（替换为实际的 UUID）
ble.select_write("0000ae41-0000-1000-8000-00805f9b34fb")
ble.select_notify("0000ae42-0000-1000-8000-00805f9b34fb")

# 设置接收回调
ble.on_receive = lambda data: print(f"收到: {data.hex(' ')}")

# 发送数据
ble.send_hex("01 02 03")
ble.send_text("AT+RST")

# 断开连接
ble.disconnect()
```

### 2. 使用事件回调

```python
from main import BLEDevice, BLEState

def on_connected():
    print("✅ 设备已连接")

def on_disconnected(reason: str):
    print(f"❌ 设备断开: {reason}")

def on_state_changed(old_state: BLEState, new_state: BLEState):
    print(f"🔄 状态变化: {old_state.value} -> {new_state.value}")

def on_receive(data: bytes):
    print(f"📨 收到数据: {data.hex(' ')}")

ble = BLEDevice("4E:2F:74:8C:F5:19")

# 注册事件回调
ble.on_connected = on_connected
ble.on_disconnected = on_disconnected
ble.on_state_changed = on_state_changed
ble.on_receive = on_receive

# 连接后会自动触发回调
if ble.connect():
    print(f"设备名称: {ble.name}")
    print(f"当前状态: {ble.state.value}")
```

### 3. 交互式测试模式

直接在命令行运行 main.py 进入交互模式：

```bash
python main.py
```

需要先修改代码中的 MAC 地址和 UUID（第 277、298、299 行），然后可以：
- 查看设备信息和服务列表
- 发送十六进制或文本数据
- 实时接收设备返回的数据

交互命令格式：
- 直接输入十六进制：`01 02 03 FF`
- 发送文本：`text:AT+RST`
- 退出：`quit`

## API 参考

### BLEDevice 类

#### 初始化

```python
ble = BLEDevice(mac_address: str)
```

参数：
- `mac_address`: BLE 设备的 MAC 地址，格式如 `"4E:2F:74:8C:F5:19"`

#### connect() -> bool

连接设备并枚举服务。

```python
if ble.connect():
    print("连接成功")
```

返回：
- `True`: 连接成功
- `False`: 连接失败

#### disconnect()

断开设备连接。

```python
ble.disconnect()
```

#### list_services() -> list[dict]

获取所有服务和特征信息。

```python
services = ble.list_services()
for svc in services:
    print(svc['uuid'])
    for char in svc['characteristics']:
        print(f"  {char['uuid']}: {char['properties']}")
```

返回结构：
```python
[
    {
        "uuid": "0000ae00-0000-1000-8000-00805f9b34fb",
        "characteristics": [
            {
                "uuid": "0000ae41-0000-1000-8000-00805f9b34fb",
                "properties": ["READ", "WRITE", "NOTIFY"],
                "handle": "15",
            }
        ]
    }
]
```

#### select_write(uuid: str) -> bool

选择写入特征。

```python
ble.select_write("0000ae41-0000-1000-8000-00805f9b34fb")
```

#### select_notify(uuid: str, on_receive: Callable[[bytes], None] = None) -> bool

选择通知特征并订阅。可以通过两种方式设置接收回调：

**方式一：** 通过参数传入
```python
def on_data(data: bytes):
    print(f"收到: {data}")

ble.select_notify("0000ae42-0000-1000-8000-00805f9b34fb", on_receive=on_data)
```

**方式二：** 通过属性设置
```python
ble.on_receive = lambda data: print(f"收到: {data.hex(' ')}")
ble.select_notify("0000ae42-0000-1000-8000-00805f9b34fb")
```

#### send(data: bytes) -> bool

发送原始字节数据。

```python
ble.send(b'\x01\x02\x03')
```

#### send_hex(hex_str: str) -> bool

发送十六进制字符串。

```python
ble.send_hex("01 02 03 FF")
ble.send_hex("01:02:03:FF")  # 也支持冒号分隔
```

#### send_text(text: str) -> bool

发送 UTF-8 文本。

```python
ble.send_text("AT+RST")
```

#### find_char(uuid: str) -> dict | None

根据 UUID 查找特征信息。

```python
char = ble.find_char("0000ae41-0000-1000-8000-00805f9b34fb")
if char:
    print(char['properties'])
```

#### reconnect() -> bool

断开后重新连接。

```python
if ble.reconnect():
    print("重连成功")
```

#### connected (property) -> bool

检查设备是否已连接。

```python
if ble.connected:
    print("设备已连接")
```

#### state (property) -> BLEState

获取当前连接状态。

```python
print(ble.state)  # BLEState.CONNECTED
print(ble.state.value)  # "connected"
```

状态枚举：
- `BLEState.DISCONNECTED` - 未连接
- `BLEState.CONNECTING` - 连接中
- `BLEState.CONNECTED` - 已连接
- `BLEState.ERROR` - 错误

### 事件回调

#### on_connected: Callable[[], None]

连接成功时触发。

```python
ble.on_connected = lambda: print("连接成功")
```

#### on_disconnected: Callable[[str], None]

断开连接时触发，参数为断开原因。

```python
ble.on_disconnected = lambda reason: print(f"断开: {reason}")
```

#### on_state_changed: Callable[[BLEState, BLEState], None]

状态变化时触发，参数为旧状态和新状态。

```python
ble.on_state_changed = lambda old, new: print(f"{old.value} -> {new.value}")
```

#### on_receive: Callable[[bytes], None]

接收到数据时触发。

```python
ble.on_receive = lambda data: print(f"收到: {data.hex(' ')}")
```

## 项目结构

```
hid_ble_AT_uart/
├── main.py                  # BLEDevice 核心类实现 + 交互式测试
├── requirements.txt         # Python 依赖
└── README.md               # 本文档
```

## 常见问题

### Q: 连接失败怎么办？

A: 检查以下几点：
1. MAC 地址是否正确
2. 设备是否已与 Windows 系统配对
3. 设备是否在有效范围内且已开启
4. 是否有其他应用占用了该设备

### Q: 为什么扫描不到我的设备？

A: 本工具设计为连接**系统已配对**的设备。如果设备是 BLE HID 类型且已连接，可能不会出现在普通扫描中。请：
1. 先在 Windows 设置中配对设备
2. 使用 MAC 地址直接连接

### Q: 如何获取设备的 UUID？

A: 连接成功后，调用 `list_services()` 查看所有服务和特征的 UUID：

```python
ble.connect()
for svc in ble.list_services():
    print(f"Service: {svc['uuid']}")
    for char in svc['characteristics']:
        print(f"  Characteristic: {char['uuid']}")
```

### Q: 收不到通知数据？

A: 确认：
1. 已正确调用 `select_notify()` 并设置了回调函数（通过参数或 `on_receive` 属性）
2. 特征的属性包含 "NOTIFY" 或 "INDICATE"
3. 设备端确实发送了通知数据

### Q: 支持哪些 Windows 版本？

A: 需要 Windows 10 或更高版本，因为使用了 WinRT API。

## 注意事项

⚠️ **重要提示**：

1. **仅支持 Windows** - 使用 WinRT API，不支持 Linux/macOS
2. **需要管理员权限** - 某些蓝牙操作可能需要提升权限
3. **设备必须已配对** - 本工具不进行配对操作，只连接已配对设备
4. **单例连接** - 同一设备不建议同时被多个应用连接
5. **线程安全** - 内部使用独立线程处理异步操作，对外提供同步接口

## 开发说明

### 架构设计

```
外部调用 (同步)
    ↓
BLEDevice 类 (同步接口)
    ↓
后台 asyncio 事件循环 (独立线程)
    ↓
WinRT API (BluetoothLEDevice, GattCharacteristic)
```

### 添加新功能

如需扩展功能，建议在 `BLEDevice` 类中添加方法，保持同步接口风格：

```python
def new_feature(self, ...):
    return self._wait(self._new_feature_async(...))

async def _new_feature_async(self, ...):
    # 异步实现
    pass
```

## 许可证

本项目仅供学习和研究使用。

## 贡献

欢迎提交 Issue 和 Pull Request！

---

**提示**: 使用前请确保已在 Windows 系统中配对好目标 BLE 设备。
