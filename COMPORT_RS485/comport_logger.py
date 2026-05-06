# Скрипт для асинхронной записи потока с COM-port
# В начале есть проверка чтения по выбранному протоколу из .bin файла, записанного предварительно
# (Активно используется)

import asyncio
from socketserver import TCPServer

import serial_asyncio
import struct
import time

from urllib3.response import GzipDecoder

PORT = "COM3"
BAUDRATE = 921600
# BAUDRATE = 115200 # для теста на Arduino UNO (работает, avg Rate: 188 pkt/s, avg Loss: 0

SYNC = b'\xAA\x55'
FRAME_SIZE  = 32
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

        # под протокол БЧЭ-500 режимов 2-3
        # нумерация байтов 0:31
        header  = struct.unpack('<H', payload[:2])[0]  # не уверен с < или >
        # чтение векторов G и A через '<f' идет с nan, выбран '<i' согласно документации
        Gx      = struct.unpack('<i', payload[2:6])[0] # берутся 2,3,4,5 байты
        Gy      = struct.unpack('<i', payload[6:10])[0]
        Gz      = struct.unpack('<i', payload[10:14])[0]
        Ax      = struct.unpack('<i', payload[14:18])[0]
        Ay      = struct.unpack('<i', payload[18:22])[0]
        Az      = struct.unpack('<i', payload[22:26])[0]
        TC      = struct.unpack('<h', payload[26:28])[0]
        Counter = payload[28] # беззнаковое
        Status  = payload[29] # беззнаковое
        CRC     = struct.unpack('>H', payload[30:32])[0]

        CRC2    = struct.unpack('<H', payload[30:32])[0]

        checkCRC = crc16_ccitt(payload[2:-2]) # все кроме CRC
        if CRC == checkCRC: CRC_STATUS = 1
        else: CRC_STATUS = 0
        print(f"[{header}] | [{Gx:6d}] [{Gy:6d}] [{Gz:6d}] | [{Ax:6d}] [{Ay:6d}] [{Az:6d}] | [{TC:6d}] | [{Counter:4d}] [{Status:3d}] | [{CRC:6d}]  | [{CRC_STATUS:1d}]")



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

            frame = self.buffer[idx:idx + FRAME_SIZE]
            self.buffer = self.buffer[idx + FRAME_SIZE:]

            #payload = frame[2:34] # получение пакета (его вырез, но в нашем случае можно не обрезать
            payload = frame
            # Распаковка пакета по протоколу
            header      = struct.unpack('<H', payload[:2])[0] # не уверен с < или >
            header_alt  = struct.unpack('<H', payload[:2])
            Gx          = struct.unpack('<f', payload[3:6])
            Gy          = struct.unpack('<f', payload[3:6])
            Gz          = struct.unpack('<f', payload[3:6])
            Ax          = struct.unpack('<f', payload[3:6])
            Ay          = struct.unpack('<f', payload[3:6])
            Az          = struct.unpack('<f', payload[3:6])
            TC          = struct.unpack('<i', payload[26:])
            Counter     = struct.unpack('<f', payload[30:32])
            Status      = struct.unpack('<f', payload[3:6])
            CRC         = struct.unpack('<f', payload[30:32])

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