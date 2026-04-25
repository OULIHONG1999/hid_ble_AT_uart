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


class SendMode(Enum):
    TEXT = "text"
    HEX = "hex"


class BLEDevice:
    """BLE 设备通信类，支持连接/断开事件回调，自动处理重连后特征刷新。"""

    DEFAULT_MTU = 247
    ATT_HEADER = 3

    def __init__(self, mac_address: str):
        self.mac_address = mac_address
        self.mac_int = int(mac_address.replace(":", ""), 16)

        self.device: BluetoothLEDevice | None = None
        self.name = ""
        self.services: list[dict] = []
        self.state = BLEState.DISCONNECTED
        self.mtu = self.DEFAULT_MTU

        # 发送模式
        self.send_mode = SendMode.TEXT

        self._write_char = None
        self._notify_char = None

        # 缓存用户选中的特征 UUID，用于重连后自动恢复
        self._write_uuid: str | None = None
        self._notify_uuid: str | None = None
        self._notify_handler = None
        self._notify_subscribed = False

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

    @property
    def max_payload(self) -> int:
        return self.mtu - self.ATT_HEADER

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
                    self._wait(self._refresh_after_reconnect())

        self.device.add_connection_status_changed(on_status_changed)

    # ── 读取 MTU ──

    def _update_mtu(self):
        try:
            session = self.device.gatt_session
            self.mtu = session.max_pdu_size
        except Exception:
            self.mtu = self.DEFAULT_MTU

    # ── 重连后刷新 ──

    async def _refresh_after_reconnect(self):
        try:
            self._update_mtu()

            result = await self.device.get_gatt_services_async()
            if result.status != GattCommunicationStatus.SUCCESS:
                return

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

            if self._write_uuid:
                self._write_char = self._find_char(self._write_uuid)

            if self._notify_uuid:
                self._notify_char = self._find_char(self._notify_uuid)
                if self._notify_char:
                    await self._subscribe_notify(self._notify_char)

        except Exception as e:
            print(f"[警告] 重连刷新失败: {e}")

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
            self._update_mtu()

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
        self._notify_subscribed = False
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
        self._write_uuid = uuid
        return True

    def select_notify(self, uuid: str, on_receive: Callable[[bytes], None] = None) -> bool:
        char = self._find_char(uuid)
        if char is None:
            return False
        self._notify_char = char
        self._notify_uuid = uuid
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
        self._notify_handler = handler
        status = await char.write_client_characteristic_configuration_descriptor_async(
            GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
        )
        self._notify_subscribed = (status == GattCommunicationStatus.SUCCESS)
        return self._notify_subscribed

    # ── 收发（分包） ──

    def send(self, data: bytes) -> bool:
        if not self.connected or self._write_char is None:
            return False
        return self._wait(self._send_async(data))

    async def _send_async(self, data: bytes) -> bool:
        payload = self.max_payload
        total = len(data)

        if total <= payload:
            return await self._write_chunk(data)

        offset = 0
        while offset < total:
            chunk = data[offset: offset + payload]
            ok = await self._write_chunk(chunk)
            if not ok:
                return False
            offset += len(chunk)
            if offset < total:
                await asyncio.sleep(0.02)
        return True

    async def _write_chunk(self, chunk: bytes) -> bool:
        try:
            writer = DataWriter()
            writer.write_bytes(chunk)
            result = await self._write_char.write_value_with_result_async(writer.detach_buffer())
            if result.status != GattCommunicationStatus.SUCCESS:
                return False
            return True
        except OSError:
            if self._write_uuid:
                new_char = self._find_char(self._write_uuid)
                if new_char:
                    self._write_char = new_char
                    try:
                        writer = DataWriter()
                        writer.write_bytes(chunk)
                        result = await self._write_char.write_value_with_result_async(writer.detach_buffer())
                        return result.status == GattCommunicationStatus.SUCCESS
                    except Exception:
                        pass
            return False

    def send_hex(self, hex_str: str) -> bool:
        hex_str = hex_str.replace(" ", "").replace(":", "")
        try:
            data = bytes.fromhex(hex_str)
        except ValueError:
            return False
        return self.send(data)

    def send_text(self, text: str) -> bool:
        return self.send(text.encode("utf-8"))

    def send_with_mode(self, raw: str) -> tuple[bool, str, int]:
        """根据当前 send_mode 发送，返回 (成功, 模式, 字节数)。"""
        if self.send_mode == SendMode.HEX:
            hex_str = raw.replace(" ", "").replace(":", "")
            try:
                data = bytes.fromhex(hex_str)
            except ValueError:
                return False, "hex", 0
            ok = self.send(data)
            return ok, "hex", len(data)
        else:
            data = raw.encode("utf-8")
            ok = self.send(data)
            return ok, "text", len(data)

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


# ── 测试入口 ──
if __name__ == "__main__":

    MAC = "4E:2F:74:8C:F5:19"
    WRITE_UUID = "0000ae41-0000-1000-8000-00805f9b34fb"
    NOTIFY_UUID = "0000ae42-0000-1000-8000-00805f9b34fb"

    def on_data(data: bytes):
        timestamp = time.strftime("%H:%M:%S.") + f"{time.time():.3f}".split(".")[-1]
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        print(f"[{timestamp}] [收到] hex={data.hex(' ')}  text={text}")

    def on_connected():
        print("[事件] >>> 设备已连接")

    def on_disconnected(reason):
        print(f"[事件] <<< 设备断开: {reason}")

    def on_state_changed(old, new):
        print(f"[状态] {old.value} -> {new.value}")

    ble = BLEDevice(MAC)
    ble.on_connected = on_connected
    ble.on_disconnected = on_disconnected
    ble.on_state_changed = on_state_changed
    ble.on_receive = on_data

    print(f"正在连接 {MAC} ...")
    if not ble.connect():
        print("连接失败，退出")
        exit(1)

    print(f"设备名称: {ble.name}")
    print(f"当前状态: {ble.state.value}")
    print(f"MTU:      {ble.mtu}  (单次最大载荷: {ble.max_payload} 字节)")

    print("\n========== 服务列表 ==========")
    for svc in ble.list_services():
        print(f"  Service: {svc['uuid']}")
        for c in svc["characteristics"]:
            print(f"    Char: {c['uuid']}  [{', '.join(c['properties'])}]  handle={c['handle']}")
    print("==============================\n")

    ok_w = ble.select_write(WRITE_UUID)
    ok_n = ble.select_notify(NOTIFY_UUID)
    print(f"写特征 {WRITE_UUID}: {'OK' if ok_w else '未找到'}")
    print(f"通知特征 {NOTIFY_UUID}: {'OK' if ok_n else '未找到'}")

    if not ok_w:
        print("写特征未找到，无法发送，请检查 UUID")
        ble.disconnect()
        exit(1)

    print()
    print("========== 通信就绪 ==========")
    print(f"  当前模式: {ble.send_mode.value}  (输入 mode 切换)")
    print(f"  MTU={ble.mtu}，单次最大载荷={ble.max_payload} 字节")
    print()
    print("  发送：")
    print("    直接输入内容，按当前模式发送")
    print("    hex:01 02 FF   → 强制 hex 模式发送本次")
    print("    text:123456    → 强制 text 模式发送本次")
    print("  命令：")
    print("    mode           → 查看当前模式")
    print("    mode hex       → 切换默认为 hex 模式")
    print("    mode text      → 切换默认为 text 模式")
    print("    status         → 查看状态")
    print("    mtu            → 重新读取 MTU")
    print("    services       → 列出服务")
    print("    reconnect      → 重连")
    print("    test           → 发送 Hello (hex)")
    print("    bigtest        → 发送 60 字节 0x11 (验证分包)")
    print("    quit           → 退出")
    print("==============================\n")

    while True:
        try:
            line = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        cmd = line.lower()

        # ── 命令 ──

        if cmd == "quit":
            break

        elif cmd == "mode":
            print(f"  当前模式: {ble.send_mode.value}")
            print(f"  切换: mode hex / mode text")
            continue

        elif cmd == "mode hex":
            ble.send_mode = SendMode.HEX
            print(f"  已切换到 hex 模式")
            print(f"  现在输入 123456 会发送 3 字节: 12 34 56")
            continue

        elif cmd == "mode text":
            ble.send_mode = SendMode.TEXT
            print(f"  已切换到 text 模式")
            print(f"  现在输入 123456 会发送 6 字节文本: '123456'")
            continue

        elif cmd == "status":
            print(f"  MAC:       {ble.mac_address}")
            print(f"  Name:      {ble.name}")
            print(f"  State:     {ble.state.value}")
            print(f"  Mode:      {ble.send_mode.value}")
            print(f"  MTU:       {ble.mtu}")
            print(f"  Max chunk: {ble.max_payload} bytes")
            print(f"  Write:     {ble._write_uuid}  (cached)")
            print(f"  Notify:    {ble._notify_uuid}  (cached)")
            print(f"  Connected: {ble.connected}")
            continue

        elif cmd == "mtu":
            ble._update_mtu()
            print(f"  MTU: {ble.mtu}  (单次最大载荷: {ble.max_payload} 字节)")
            continue

        elif cmd == "services":
            if not ble.connected:
                print("  设备未连接")
                continue
            for svc in ble.list_services():
                print(f"  Service: {svc['uuid']}")
                for c in svc["characteristics"]:
                    print(f"    {c['uuid']}  [{', '.join(c['properties'])}]")
            continue

        elif cmd == "reconnect":
            print("  正在重连...")
            ok = ble.reconnect()
            print(f"  重连{'成功' if ok else '失败'}")
            if ok:
                print(f"  设备名称: {ble.name}")
                print(f"  MTU:      {ble.mtu}")
                print(f"  写特征:   {'已恢复' if ble._write_char else '未恢复'}")
                print(f"  通知特征: {'已恢复' if ble._notify_char else '未恢复'}")
            continue

        elif cmd == "test":
            test_data = b"Hello"
            print(f"  发送: {test_data.hex(' ')} (Hello)  [{len(test_data)} 字节]")
            ok = ble.send(test_data)
            print(f"  结果: {'成功' if ok else '失败'}")
            continue

        elif cmd == "bigtest":
            test_data = bytes([0x11] * 60)
            chunks = -(-len(test_data) // ble.max_payload)
            print(f"  发送: {len(test_data)} 字节 0x11  (将分 {chunks} 包)")
            ok = ble.send(test_data)
            print(f"  结果: {'成功' if ok else '失败'}")
            continue

        # ── 发送 ──

        if line.startswith("text:"):
            raw = line[5:]
            data = raw.encode("utf-8")
            print(f"  [text 强制] {len(data)} 字节", end=" ")
            if len(data) > ble.max_payload:
                print(f"(分 {-(-len(data)//ble.max_payload)} 包)", end=" ")
            ok = ble.send(data)
            mode = "text"

        elif line.startswith("hex:"):
            raw = line[4:]
            hex_str = raw.replace(" ", "").replace(":", "")
            try:
                data = bytes.fromhex(hex_str)
            except ValueError:
                print("  hex 格式错误")
                continue
            print(f"  [hex 强制] {len(data)} 字节", end=" ")
            if len(data) > ble.max_payload:
                print(f"(分 {-(-len(data)//ble.max_payload)} 包)", end=" ")
            ok = ble.send(data)
            mode = "hex"

        else:
            ok, mode, byte_count = ble.send_with_mode(line)
            print(f"  [{mode}] {byte_count} 字节", end=" ")
            if byte_count > ble.max_payload:
                print(f"(分 {-(-byte_count//ble.max_payload)} 包)", end=" ")

        if not ok:
            if not ble.connected:
                print(f"\n  设备未连接，输入 reconnect 尝试重连")
            else:
                print(f"\n  发送失败 (模式: {mode})")
        else:
            print(f"发送成功")

    ble.disconnect()
    print("已断开，退出")
