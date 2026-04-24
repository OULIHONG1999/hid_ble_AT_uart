import asyncio
import threading
import time
from enum import Enum
from typing import Callable

from winsdk.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus
from winsdk.windows.devices.bluetooth.genericattributeprofile import (
    GattClientCharacteristicConfigurationDescriptorValue,
    GattCommunicationStatus,
)
from winsdk.windows.storage.streams import DataWriter, DataReader


class BLEState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class BLEDevice:
    """BLE 设备通信类，支持连接/断开事件回调。"""

    def __init__(self, mac_address: str):
        self.mac_address = mac_address
        self.mac_int = int(mac_address.replace(":", ""), 16)

        self.device: BluetoothLEDevice | None = None
        self.name = ""
        self.services: list[dict] = []
        self.state = BLEState.DISCONNECTED

        self._write_char = None
        self._notify_char = None

        # 防抖
        self._last_connected_time = 0.0
        self._disconnect_debounce = 5.0

        # 事件回调
        self.on_connected: Callable[[], None] | None = None
        self.on_disconnected: Callable[[str], None] | None = None
        self.on_state_changed: Callable[[BLEState, BLEState], None] | None = None
        self.on_receive: Callable[[bytes], None] | None = None

        # 后台事件循环
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    # ── 内部基础设施 ──

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro) -> asyncio.Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _wait(self, coro):
        return self._submit(coro).result()

    def _set_state(self, new_state: BLEState):
        old_state = self.state
        if old_state == new_state:
            return
        self.state = new_state
        if self.on_state_changed:
            try:
                self.on_state_changed(old_state, new_state)
            except Exception:
                pass

    # ── 连接状态监听 ──

    def _watch_device_status(self):
        if self.device is None:
            return

        def on_status_changed(sender, args):
            status = sender.connection_status
            now = time.monotonic()

            if status == BluetoothConnectionStatus.DISCONNECTED:
                if now - self._last_connected_time < self._disconnect_debounce:
                    return
                self._set_state(BLEState.DISCONNECTED)
                if self.on_disconnected:
                    try:
                        self.on_disconnected("设备断开（超出范围或关机）")
                    except Exception:
                        pass
            elif status == BluetoothConnectionStatus.CONNECTED:
                self._last_connected_time = now
                if self.state != BLEState.CONNECTED:
                    self._set_state(BLEState.CONNECTED)

        self.device.add_connection_status_changed(on_status_changed)

    # ── 连接 ──

    def connect(self) -> bool:
        return self._wait(self._connect_async())

    async def _connect_async(self) -> bool:
        self._set_state(BLEState.CONNECTING)

        try:
            self.device = await BluetoothLEDevice.from_bluetooth_address_async(self.mac_int)
            if self.device is None:
                self._set_state(BLEState.ERROR)
                if self.on_disconnected:
                    self.on_disconnected("设备未找到")
                return False

            self.name = self.device.name

            result = await self.device.get_gatt_services_async()
            if result.status != GattCommunicationStatus.SUCCESS:
                self._set_state(BLEState.ERROR)
                if self.on_disconnected:
                    self.on_disconnected(f"获取服务失败: {result.status}")
                return False

            self.services = []
            for service in result.services:
                chars_result = await service.get_characteristics_async()
                chars = []
                for char in chars_result.characteristics:
                    chars.append({
                        "uuid": str(char.uuid),
                        "properties": self._parse_props(char.characteristic_properties),
                        "handle": self._get_handle(char),
                        "_char": char,
                    })
                self.services.append({
                    "uuid": str(service.uuid),
                    "characteristics": chars,
                })

            self._last_connected_time = time.monotonic()
            self._set_state(BLEState.CONNECTED)
            if self.on_connected:
                self.on_connected()

            self._watch_device_status()
            return True

        except Exception as e:
            self._set_state(BLEState.ERROR)
            if self.on_disconnected:
                self.on_disconnected(f"连接异常: {e}")
            return False

    # ── 断开 ──

    def disconnect(self, reason: str = "主动断开"):
        was_connected = self.connected
        if self.device:
            self.device.close()
            self.device = None
        self._write_char = None
        self._notify_char = None
        self._set_state(BLEState.DISCONNECTED)
        if was_connected and self.on_disconnected:
            self.on_disconnected(reason)

    @property
    def connected(self) -> bool:
        return self.state == BLEState.CONNECTED

    # ── 重连 ──

    def reconnect(self) -> bool:
        self.disconnect(reason="重连中断开")
        return self.connect()

    # ── 选择特征 ──

    def select_write(self, uuid: str) -> bool:
        char = self._find_char(uuid)
        if char is None:
            return False
        self._write_char = char
        return True

    def select_notify(self, uuid: str, on_receive: Callable[[bytes], None] = None) -> bool:
        char = self._find_char(uuid)
        if char is None:
            return False
        self._notify_char = char
        if on_receive:
            self.on_receive = on_receive
        return self._wait(self._subscribe_notify(char))

    async def _subscribe_notify(self, char) -> bool:
        def handler(sender, args):
            reader = DataReader.from_buffer(args.characteristic_value)
            length = reader.unconsumed_buffer_length
            data = bytearray()
            for _ in range(length):
                data.append(reader.read_byte())
            if self.on_receive:
                try:
                    self.on_receive(bytes(data))
                except Exception:
                    pass

        char.add_value_changed(handler)
        status = await char.write_client_characteristic_configuration_descriptor_async(
            GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
        )
        return status == GattCommunicationStatus.SUCCESS

    # ── 收发 ──

    def send(self, data: bytes) -> bool:
        if not self.connected or self._write_char is None:
            return False
        return self._wait(self._send_async(data))

    async def _send_async(self, data: bytes) -> bool:
        writer = DataWriter()
        writer.write_bytes(data)
        result = await self._write_char.write_value_with_result_async(writer.detach_buffer())
        return result.status == GattCommunicationStatus.SUCCESS

    def send_hex(self, hex_str: str) -> bool:
        hex_str = hex_str.replace(" ", "").replace(":", "")
        try:
            data = bytes.fromhex(hex_str)
        except ValueError:
            return False
        return self.send(data)

    def send_text(self, text: str) -> bool:
        return self.send(text.encode("utf-8"))

    def send_auto(self, text: str) -> tuple[bool, str]:
        """智能发送：自动判断 hex 还是文本。
        返回 (是否成功, 实际发送模式)。
        纯 hex 字符串走 hex，否则走文本。
        """
        stripped = text.replace(" ", "").replace(":", "")
        if stripped and all(c in "0123456789abcdefABCDEF" for c in stripped) and len(stripped) % 2 == 0:
            return self.send_hex(text), "hex"
        else:
            return self.send_text(text), "text"

    # ── 查询 ──

    def list_services(self) -> list[dict]:
        return self.services

    def find_char(self, uuid: str) -> dict | None:
        for svc in self.services:
            for char in svc["characteristics"]:
                if char["uuid"] == uuid:
                    return char
        return None

    # ── 工具 ──

    def _find_char(self, uuid: str):
        info = self.find_char(uuid)
        return info["_char"] if info else None

    @staticmethod
    def _parse_props(props: int) -> list[str]:
        result = []
        if props & 0x02:
            result.append("READ")
        if props & 0x04:
            result.append("WRITE_NO_RESP")
        if props & 0x08:
            result.append("WRITE")
        if props & 0x10:
            result.append("NOTIFY")
        if props & 0x20:
            result.append("INDICATE")
        return result

    @staticmethod
    def _get_handle(char) -> str:
        try:
            return str(char.attribute_handle)
        except Exception:
            return "N/A"


# ── 使用示例 ──
if __name__ == "__main__":

    def on_data(data: bytes):
        print(f"[收到] {data.hex(' ')}  |  {data}")

    ble = BLEDevice("4E:2F:74:8C:F5:19")

    ble.on_connected = lambda: print("[事件] 设备已连接")
    ble.on_disconnected = lambda reason: print(f"[事件] 设备断开: {reason}")
    ble.on_state_changed = lambda old, new: print(f"[状态] {old.value} -> {new.value}")
    ble.on_receive = on_data

    if not ble.connect():
        print("连接失败")
        exit()

    print(f"设备名称: {ble.name}")
    print(f"当前状态: {ble.state.value}")

    print("\n服务列表：")
    for svc in ble.list_services():
        print(f"  Service: {svc['uuid']}")
        for c in svc["characteristics"]:
            print(f"    {c['uuid']}  [{', '.join(c['properties'])}]")

    ble.select_write("0000ae41-0000-1000-8000-00805f9b34fb")
    ble.select_notify("0000ae42-0000-1000-8000-00805f9b34fb")

    print(f"\n通信就绪，直接输入内容即可发送：")
    print("  纯 hex 字符串自动走 hex 模式（如 01 02 FF）")
    print("  其他内容自动走文本模式（如 你好、AT+RST）")
    print("  显式指定: text:你好 或 hex:01 02")
    print("  退出: quit")
    print()

    while True:
        line = input(">>> ").strip()
        if not line:
            continue
        if line.lower() == "quit":
            break

        if line.startswith("text:"):
            ok = ble.send_text(line[5:])
            mode = "text"
        elif line.startswith("hex:"):
            ok = ble.send_hex(line[4:])
            mode = "hex"
        else:
            ok, mode = ble.send_auto(line)

        if not ok:
            if not ble.connected:
                print("设备未连接")
            else:
                print(f"发送失败（模式: {mode}）")
        else:
            print(f"发送成功（模式: {mode}）")

    ble.disconnect()
    print("已断开")
