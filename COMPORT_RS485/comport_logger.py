# Скрипт для асинхронной записи потока с COM-port
# В начале есть проверка чтения по выбранному протоколу из .bin файла, записанного предварительно
# (Активно используется)

import asyncio
from socketserver import TCPServer
from time import gmtime

import serial_asyncio
import struct
import time

from urllib3.response import GzipDecoder

PORT = "COM3"
BAUDRATE = 921600
# BAUDRATE = 115200 # для теста на Arduino UNO (работает, avg Rate: 188 pkt/s, avg Loss: 0

SYNC = b'\xAA\x55'
FRAME_SIZE = 32
#FRAME_SIZE += 2 # 32+2

def crc16_ccitt(data, poly=0x1021, init=0xFFFF):
    crc = init
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc

# закомментировать при реальном подключении
with open("bindata/IMU500_static.bin", "rb") as f:
    while True:
        chunk = f.read(FRAME_SIZE)
        if len(chunk) < FRAME_SIZE:
            break
        payload = chunk # остается только здесь для примера

        # распаковка под протокол БЧЭ-500 режимов 2-3
        [header, Gx, Gy, Gz, Ax, Ay, Az, TC, Counter, Status, CRC] = struct.unpack('<H6ih2BH', payload)
        # коэффициенты для удобного отображения
        Gmult = 1.085069*10**(-6) # град/с
        Amult = 5*10**(-5) # м/с^2
        Tmult = 0.01 # °C

        checkCRC = crc16_ccitt(payload[2:-2]) # все кроме CRC
        if CRC == checkCRC: CRC_STATUS = 1
        else: CRC_STATUS = 0

        print(f"[{header}] | [{Gx:6d}] [{Gy:6d}] [{Gz:6d}] | [{Ax:6d}] [{Ay:6d}] [{Az:6d}] | [{TC:6d}] | [{Counter:3d}] [{Status:3d}] | [{CRC:6d}]  | [{CRC_STATUS:1d}]")
        # вывод с переводом в удобные величины
        #Gx, Gy, Gz = Gx * Gmult, Gy * Gmult, Gz * Gmult
        #Ax, Ay, Az = Ax * Amult, Ay * Amult, Az * Gmult
        #TC *= Tmult
        #print(f"[{header}] | [{Gx:5.3f} град/с] [{Gy:5.3f} град/с] [{Gz:5.3f} град/с] | [{Ax:5.3f} м/с^2] [{Ay:5.3f} м/с^2] [{Az:5.3f} м/с^2] | [{TC:5.3f} °C] | [{Counter:4d}] [{Status:3d}] | [{CRC:6d}]  | [{CRC_STATUS:1d}]")

class SerialProtocol(asyncio.Protocol): # асинхронный протокол
    def __init__(self, file):
        self.buffer = b''
        self.file = file

        self.prev_counter = None
        self.loss_count = 0
        self.total_packets = 0

        self.timestamps = []

        self.last_stat_time = time.time()

    def connection_made(self, transport):
        self.transport = transport
        print("[v] Connected")

    def data_received(self, data): # Важно переписать под структуру протокола
        self.buffer += data

        while True: # !!! бесконечный цикл

            idx = self.buffer.find(SYNC)
            if idx == -1:
                self.buffer = self.buffer[-2:]
                break

            if len(self.buffer) < idx + FRAME_SIZE:
                break

            payload =     self.buffer[idx:idx + FRAME_SIZE]
            self.buffer = self.buffer[idx + FRAME_SIZE:]

            # распаковка под протокол БЧЭ-500 режимов 2-3
            [header, Gx, Gy, Gz, Ax, Ay, Az, TC, Counter, Status, CRC] = struct.unpack('<H6ih2BH', payload)
            # коэффициенты для удобного отображения
            Gmult = 1.085069 * 10 ** (-6)  # град/с
            Amult = 5 * 10 ** (-5)  # м/с^2
            Tmult = 0.01  # °C

            checkCRC = crc16_ccitt(payload[2:-2])  # все кроме CRC
            if CRC == checkCRC:
                CRC_STATUS = 1
            else:
                CRC_STATUS = 0
            return
"""     
        # Эти проверки актуальны для симуляции COM порта
        if self.prev_counter is not None:
                if counter != self.prev_counter + 1:
                    self.loss_count += (counter - self.prev_counter - 1)

            self.prev_counter = counter
            self.total_packets += 1

            ts = time.time_ns()
            self.timestamps.append(ts)

            self.file.write(struct.pack('<Q', ts))
            self.file.write(payload)

        self.print_stats()

    def print_stats(self):
        now = time.time()

        if now - self.last_stat_time >= 1:
            if len(self.timestamps) > 1:
                dt = (self.timestamps[-1] - self.timestamps[0]) / 1e9
                rate = len(self.timestamps) / dt if dt > 0 else 0
            else:
                rate = 0

            print(f"Rate: {rate:.0f} pkt/s | Loss: {self.loss_count}")

            self.timestamps.clear()
            self.last_stat_time = now
"""

async def main():
    loop = asyncio.get_running_loop()

    with open("data_async.bin", "wb") as f:
        transport, protocol = await serial_asyncio.create_serial_connection(
            loop,
            lambda: SerialProtocol(f),
            PORT,
            baudrate=BAUDRATE
        )

        try:
            await asyncio.Future()  # run forever
        finally:
            transport.close()


if __name__ == "__main__":
    asyncio.run(main())