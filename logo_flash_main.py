import PyQt5
from PyQt5.QtWidgets import QMainWindow, QApplication, QListWidgetItem, QFileDialog, QMessageBox
from PyQt5.QtGui import QPixmap, QMovie
from PyQt5.QtCore import QObject, pyqtSlot, pyqtSignal, QRunnable, QThreadPool, Qt, QMutex, QWaitCondition, QMutexLocker
from PyQt5 import QtCore, QtWidgets, QtGui
from logo_flash_ui import Ui_MainWindow
import sys
import os
import serial
import time
import crc8
from math import ceil
from datetime import datetime
from PIL import Image, ImageQt
import numpy as np
from writeCFile import writeCFile
from serial.tools import list_ports
from logs import fileTransfer, error, printCommand, sendCommand, imageConvert
import csv
import winsound
# scale with high DPI
QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)
################################################################################################

OK = "OK"
TIMEOUT = "TIMEOUT"
CMD = "CMD"
MSG = "MSG"
ERROR = "ERROR"
SUCCESS = "SUCCESS"
READY = "READY"
FAIL = "FAIL"
DELETE_ACK = "1C6100"
CHUNK_SUCCESS = "1C5100"
CHUNK_LOGO_INDEX_UNAVAILABLE = "1C5101"
CHUNK_LOGO_EXISTS = "1C5102"
CHUNK_LOGO_INDEX_INVALID = "1C5103"
CHUNK_LOGO_WRITE_ERROR = "1C5104"
CHUNK_LOGO_GENERIC_ERROR = "1C5105"
DELAY = 0.01


def get_path(file):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, file)
    else:
        return file


class ComWorkerFlashSignals(QObject):
    serial_available = pyqtSignal(object)
    error = pyqtSignal(object)


class ComWorkerFlash(QRunnable):
    def __init__(self, port=None, timeout=10):
        super().__init__()
        self.signals = ComWorkerFlashSignals()
        self.port = port
        self.timeout = timeout

    @pyqtSlot()
    def run(self):
        self.setup_serial_com()

    def setup_serial_com(self):
        '''
        Create Serial Object with the selected port
        '''
        try:
            ser = serial.Serial(port=self.port, parity=serial.PARITY_NONE,
                                stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS,
                                timeout=0.1, write_timeout=self.timeout
                                )
            self.signals.serial_available.emit(ser)
        except serial.SerialException as serialException:
            error.exception(serialException)
            self.signals.error.emit(serialException)
        except serial.SerialTimeoutException as serialTimeout:
            error.exception(serialTimeout)
            self.signals.error.emit(serialTimeout)


class pushCommandSignals(QObject):
    error = pyqtSignal(tuple)
    response = pyqtSignal(tuple)
    finished = pyqtSignal()
    serial = pyqtSignal(object)


class pushCommand(QRunnable):
    def __init__(self, serial, cmd, port=None, timeout=10):
        super().__init__()
        self.ser = serial
        self.cmd = cmd
        self.port = port
        self.timeout = timeout
        self.signals = pushCommandSignals()

    @pyqtSlot()
    def run(self):
        '''
        Function to Send Init command
        :param: None
        :return: None
        '''
        try:
            sent = self.ser.write(bytes.fromhex(self.cmd))
            msg = f"Command | {self.cmd} | Length {str(sent)} Bytes |"
            self.signals.response.emit((msg, MSG))
            self.ser.flush()
            # time.sleep(0.1)
            if self.ser.baudrate == 115200:
                pass
            else:
                self.ser.close()
                print(self.port)
                msg = f"Port {self.port} Closed"
                self.signals.response.emit((msg, MSG))
                self.serial_com()
                msg = f"BaudRate set to {self.ser.baudrate}"
                self.signals.response.emit((msg, MSG))
            msg = f"Awaiting MCU RESPONSE"
            self.signals.response.emit((msg, MSG))
            ok = self.awaitResponse()
            if isinstance(ok, Exception):
                msg = f"Something Went Wrong | {ok}"
                self.signals.response.emit((msg, CMD))
            elif isinstance(ok, bytes):
                response = ok.decode('ASCII')
                if response == "READY":
                    msg = response
                    self.signals.response.emit((msg, CMD))
                else:
                    msg = f"{response}"
                    self.signals.response.emit((msg, CMD))
            elif isinstance(ok, bool):
                msg = "TIMEOUT"
                self.signals.response.emit((msg, CMD))
            elif isinstance(ok, str):
                msg = f"{ok} Aborting!"
                self.signals.response.emit((msg, CMD))
        except Exception as e:
            error.exception(e)
            self.signals.error.emit(("Something Went Wrong", f"{e}"))
        self.signals.finished.emit()

    def serial_com(self):
        '''
        Create Serial Object to a port
        :param: None
        :Return: Serial Object or Exception
        '''
        try:
            self.ser = serial.Serial(port=self.port, baudrate=115200, parity=serial.PARITY_NONE,
                                     stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS,
                                     timeout=0.1, write_timeout=self.timeout
                                     )
            self.signals.serial.emit(self.ser)
            print(self.ser)
            print(self.port)
        except serial.SerialException as serialException:
            error.exception(serialException)
            self.signals.error.emit(serialException)
        except serial.SerialTimeoutException as serialTimeout:
            error.exception(serialTimeout)
            self.signals.error.emit(serialTimeout)

    def awaitResponse(self):
        '''
        Read Input Buffer
        :param: None
        :Return: Exception or Bool or bytes or str
        '''
        try:
            if self.timeout:
                start = time.time()
            while True:
                if self.ser.is_open:
                    if self.ser.in_waiting > 0:
                        buffer = self.ser.read(50)
                        if len(buffer):
                            self.ser.flush()
                            return buffer
                else:
                    break
                time.sleep(DELAY)
                if self.timeout:
                    if (time.time() - start) > self.timeout:
                        return True
            return "PORT_CLOSED"
        except Exception as e:
            error.exception(e)
            return e


class fileTransferSignals(QObject):
    progress = pyqtSignal(float)
    finished = pyqtSignal(tuple)
    response = pyqtSignal(tuple)
    error = pyqtSignal(tuple)


class File:
    def __init__(self, file, ser,
                 signals, delay=0,
                 timeout=10,
                 chunk=512):
        self.file = file
        self.ser = ser
        self.signals = signals
        self.chunk = chunk
        self.timeout = timeout
        self.delay = delay

    def fileTransfer(self):
        '''
        Function to transfer the file to MCU
        CRC8 employed
        :param: None
        :return: Bool or Exception
        '''
        try:
            bytes_sent = 0
            init_bytes = "46 57 3E "
            file_offset = 0
            end_bytes = "3C 53 54 50"
            chunk_no = 1
            self.size = os.path.getsize(self.file)
            with open(self.file, 'rb') as f:
                while True:
                    data = f.read(self.chunk)
                    if len(data) > 0:
                        hash = crc8.crc8()
                        hash.update(data)
                        _crc8 = int(hash.hexdigest(), 16)
                        chunk_crc8 = bytes.fromhex(
                            "{0:08X}".format(_crc8)
                        ).hex(" ", 1).upper()
                        chunk_size = bytes.fromhex(
                            "{0:08X}".format(len(data))
                        ).hex(" ", 1).upper()
                        file_offset_hex = bytes.fromhex(
                            "{0:08X}".format(file_offset)
                        ).hex(" ", 1).upper()
                        fimware_data_command = init_bytes + file_offset_hex + \
                            chunk_size + chunk_crc8 +\
                            data.hex(" ", 1).upper() + end_bytes
                        file_offset += self.chunk

                        msg = f"{datetime.now()} : Client==> Baud Rate - {self.ser.baudrate}"
                        self.signals.response.emit((msg, OK))
                        self.ser.flush()
                        self.ser.write(bytes.fromhex(fimware_data_command))
                        self.ser.flush()
                        msg = f"{datetime.now()} : Client==> Chunk - {chunk_no} Sent"
                        self.signals.response.emit((msg, OK))

                        ok = self.awaitResponse()
                        if isinstance(ok, bytes):
                            try:
                                str_value = ok.decode('ASCII')
                                if str_value == "OK":
                                    msg = f"{datetime.now()} : Device==> Response: {str_value}"
                                    self.signals.response.emit((msg, OK))
                                    bytes_sent += len(data)
                                    per_sent = (bytes_sent/self.size)*100
                                    self.signals.progress.emit(per_sent)
                                    chunk_no += 1
                                    # with open("binary.bin","ab") as bin:
                                    #     bin.write(data)
                                    # with open("hex_data.hex","a") as hex:
                                    #     hex.write(data.hex(" ",1).upper())

                                elif str_value == "CRC ERROR":
                                    msg = f"{datetime.now()} : Device==> Response: {str_value} | \
                                            Aborting File Transfer! | chunk {chunk_no} | {str_value}"
                                    self.signals.error.emit((msg, ERROR))
                                    return False

                                elif str_value == "ERROR":
                                    msg = f"{datetime.now()} : Device==> Response: {str_value} | \
                                            Aborting File Transfer! | chunk {chunk_no} | {str_value}"
                                    self.signals.error.emit((msg, ERROR))
                                    return False

                                else:
                                    msg = f"{datetime.now()} : Device==> Response: {str_value} | \
                                            Aborting File Transfer! | chunk {chunk_no} | {str_value}"
                                    self.signals.error.emit((msg, ERROR))
                                    return False

                            except Exception as e:
                                msg = f"{datetime.now()} : Device==> Response: {ok} | \
                                        Aborting File Transfer! | chunk {chunk_no} | {e}"
                                self.signals.error.emit((msg, ERROR))
                                error.exception(e)
                                return False

                        elif isinstance(ok, bool):
                            msg = f"{datetime.now()} : Client==> Response Timed out | \
                                            Aborting File Transfer! | chunk {chunk_no}"
                            self.signals.error.emit((msg, TIMEOUT))
                            return False

                        elif isinstance(ok, Exception):
                            msg = f"{datetime.now()} : Device==> Something Went Wrong! | \
                                    Aborting File Transfer! | chunk {chunk_no} | {ok}"
                            self.signals.error.emit((msg, ERROR))
                            return ok

                        elif isinstance(ok, str):
                            msg = f"{datetime.now()} : Client==> {ok} | \
                                            Aborting File Transfer! | chunk {chunk_no}"
                            self.signals.error.emit((msg, ERROR))
                            return False
                    else:
                        break

                    if self.delay > 0:
                        time.sleep(self.delay)
            return True

        except Exception as e:
            msg = f"{datetime.now()} : Device==> Something Went Wrong! | \
                    Aborting File Transfer! | chunk {chunk_no} | {e}"
            self.signals.error.emit((msg, ERROR))
            error.exception(e)
            return e

    def awaitResponse(self):
        '''
        Function to read input buffer from MCU
        :Param: None
        :return: Bool or str or Exception or Bytes
        '''
        try:
            if self.timeout:
                start = time.time()
            while True:
                if self.ser.is_open:
                    if self.ser.in_waiting > 0:
                        buffer = self.ser.read(50)
                        if len(buffer):
                            self.ser.flush()
                            return buffer
                else:
                    break
                time.sleep(DELAY)
                if self.timeout:
                    if (time.time() - start) > self.timeout:
                        return True
            return "Port closed"
        except Exception as e:
            error.exception(e)
            return e


class WindowSoundSignals(QObject):
    finished = pyqtSignal()


class WindowSoundWorker(QRunnable):
    def __init__(self, file=None, beep=None, beepTones=None):
        super().__init__()
        self.file = file
        self.beep = beep
        self.beepTones = beepTones
        self.signals = WindowSoundSignals()

    @pyqtSlot()
    def run(self):
        if self.beep and self.beepTones:
            for _ in range(self.beepTones):
                winsound.Beep(440, 1000)
                time.sleep(0.2)
        elif self.file:
            winsound.PlaySound(self.file, winsound.SND_ALIAS)
        self.signals.finished.emit()


class fileTransferWorker(QRunnable):
    def __init__(self, ser, file, delay=0, timeout=10, chunk=512):
        super().__init__()
        self.ser = ser
        self.file = file
        self.timeout = timeout
        self.delay = delay
        self.chunk = chunk
        self.signals = fileTransferSignals()

    @pyqtSlot()
    def run(self):
        '''
        Initializes file Transfer
        :param: None
        :return:  None
        '''
        file = File(self.file, self.ser,
                    self.signals, self.delay,
                    self.timeout,
                    self.chunk
                    )

        msg = f"{datetime.now()} : Client==> Initiating File Tansfer!"
        self.signals.response.emit((msg, ""))
        response = file.fileTransfer()
        if isinstance(response, bool):
            if response:
                msg = f"{datetime.now()} : Client==> File Transfer Success"
                # self.signals.response.emit((msg,SUCCESS))
                self.signals.finished.emit((msg, SUCCESS))
            else:
                msg = f"{datetime.now()} : Client==> File Transfer Failed!"
                # self.signals.error.emit((msg,ERROR))
                self.signals.finished.emit((msg, FAIL))

        elif isinstance(response, Exception):
            msg = f"{datetime.now()} : Client==> File Transfer Failed! | {response}"
            self.signals.finished.emit((msg, FAIL))
        else:
            msg = f"{datetime.now()} : Client==> File Transfer Failed! | {response}"
            self.signals.finished.emit((msg, FAIL))


# ###########################################################################################


def extract_img_indexes(csvFilePath):
    img_obj = {}
    columns = ['LogoName', 'Index', 'Reference']
    try:
        with open(csvFilePath, 'r', encoding='utf-8') as file:
            reader = csv.reader(file, delimiter=',')
            for row in reader:
                if row != columns:
                    img_obj[row[0]] = [row[1], row[2]]
        return img_obj
    except Exception as e:
        error.exception(e)
        return e


def LogoToBytes(filePath, max_img_width):
    """
    Convert BMP logo files into Hex Bytes & Binary ( byte) Array
    :param: filePath: path to the file
    :param: max_img_width: maximum allowed width for a img file
    :return: Tuple (Hex Bytes,height,width,Qt_Image) 
    """
    try:
        # Debug Log
        imageConvert.debug(f"{filePath}")
        # Read Image
        img_data = Image.open(filePath)
        file = os.path.basename(filePath)
        fileName, ext = file.split(".")
        fileName = fileName.replace(" ", "_")

        # convert to Mono Chrome without dither
        img_arr = img_data.convert('1', dither=Image.Dither.NONE)
        img_arr = np.array(img_arr)
        height, org_width = img_arr.shape

        # Adding Padding if the image is smaller than max width
        if org_width > max_img_width:
            return [False, filePath]

        padding = max_img_width - org_width
        if padding % 2 == 0:
            pad_left = int(padding / 2)
            pad_right = int(padding / 2)
        else:
            pad_left = round(padding / 2)
            pad_right = padding - pad_left

        img_arr = np.pad(img_arr, pad_width=((0, 0), (pad_left, pad_right)),
                         mode='constant', constant_values=1)
        height, width = img_arr.shape

        # Convert Numpy Image Array to Qt Image
        qt_img_arr = Image.fromarray(img_arr)
        qt_image = ImageQt.ImageQt(qt_img_arr)

        # Debug log
        imageConvert.debug(f"height: {height} Width: {width}")
        bytesarray = []
        binaryarray = []
        offset = 8  # horizontal mapping 8 pixels/ byte
        for row in img_arr:
            for col in range(0, len(row), offset):
                # initiate binary string
                bin = ""
                for i in row[col:offset+col]:
                    # light up the pixel if 1 else 0
                    if int(i):
                        bin += "0"
                    else:
                        bin += "1"
                _hex = "{0:02X}".format(int(bin, 2))
                bytesarray.append(_hex)
                binaryarray.append(bin)
        imageConvert.debug(f"Image Buffer Length {len(bytesarray)}")
        writeCFile(width, height, filePath, bytesarray, fileName, ext)
    except Exception as e:
        error.exception(f"{e}")
        return [e, filePath]
    return (bytesarray, height, width, qt_image, filePath)


class ImageConvertSignals(QObject):
    finished = pyqtSignal()
    progress = pyqtSignal(str, list)


class ImageConvertWorker(QRunnable):
    def __init__(self, images):
        super().__init__()
        self.images = images
        self.mutex = QMutex()
        self.wait = QWaitCondition()
        self.signals = ImageConvertSignals()

    def run(self):
        for imgPath, idx_ref in self.images.items():
            self.signals.progress.emit(imgPath, idx_ref)
            self.mutex.lock()
            self.wait.wait(self.mutex)
            self.mutex.unlock()

        self.signals.finished.emit()

    def wake(self):
        self.wait.wakeOne()


class bulkTransferSignals(QObject):
    finished = pyqtSignal(str)
    imgStart = pyqtSignal(tuple)
    progress = pyqtSignal(tuple)
    error = pyqtSignal(tuple)


class bulkTransferWorker(QRunnable):
    def __init__(self, Images=[]):
        super().__init__()
        self.Images = Images
        self.signals = bulkTransferSignals()
        self.mutex = QMutex()
        self.wait = QWaitCondition()
        self.transfer_failed = False

    def run(self):
        count = 0
        for image, idx_ref in self.Images:
            if self.transfer_failed:
                break
            self.signals.imgStart.emit((image, idx_ref))
            self.mutex.lock()
            self.wait.wait(self.mutex)
            self.mutex.unlock()
            count += 1
            pbar = ceil(count/len(self.Images)*100)
            self.signals.progress.emit((count, pbar, len(self.Images)))
        if self.transfer_failed:
            self.signals.finished.emit(FAIL)
        else:
            self.signals.finished.emit(SUCCESS)

    def wake(self):
        self.wait.wakeOne()

    def fail(self):
        self.transfer_failed = True


class CsvExtractorSignals(QObject):
    lines = pyqtSignal(object)
    error = pyqtSignal(object)


class CsvExtractorWorker(QRunnable):
    def __init__(self, path=None):
        super().__init__()
        self.path = path
        self.signals = CsvExtractorSignals()

    @pyqtSlot()
    def run(self):
        df = extract_img_indexes(self.path)
        if isinstance(df, Exception):
            self.signals.error.emit(df)
        else:
            self.signals.lines.emit(df)


class LogoCreatorSignals(QObject):
    dataReady = pyqtSignal(tuple)
    error = pyqtSignal(list)


class LogoCreatorWorker(QRunnable):
    def __init__(self, path=None, max_width=576):
        super().__init__()
        self.path = path
        self.max_width = max_width
        self.signals = LogoCreatorSignals()

    @pyqtSlot()
    def run(self):
        data = LogoToBytes(self.path, self.max_width)
        if isinstance(data, tuple):
            self.signals.dataReady.emit(data)
        elif isinstance(data[0], Exception):
            self.signals.error.emit(data)
        elif isinstance(data[0], bool):
            self.signals.error.emit(data)


class ComWorkerSignals(QObject):
    serial_obj = pyqtSignal(object)
    error = pyqtSignal(object)


class ComWorker(QRunnable):
    def __init__(self, port=None, timeout=1):
        super().__init__()
        self.port = port
        self.timeout = timeout
        self.signals = ComWorkerSignals()

    @pyqtSlot()
    def run(self):
        self.setupSerialCom(self.port)

    def setupSerialCom(self, port=None):
        try:
            ser = serial.Serial(port=port, parity=serial.PARITY_NONE,
                                stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS,
                                timeout=0.1, write_timeout=self.timeout)
        except serial.SerialException as e:
            error.exception(e)
            self.signals.error.emit(e)
        except serial.SerialTimeoutException as timeout:
            error.exception(timeout)
            self.signals.error.emit(timeout)
        self.signals.serial_obj.emit(ser)


class PrinterWorkerSignals(QObject):
    finished = pyqtSignal()
    response = pyqtSignal(tuple)
    error = pyqtSignal(tuple)


class PrinterWorker(QRunnable):
    def __init__(self, serial_obj, data, timeout=10):
        super().__init__()
        self.serial_obj = serial_obj
        self.data = data
        self.timeout = timeout
        self.signals = PrinterWorkerSignals()
        self.response_code = "50"
        self.print_setupCode = "FFFF81"

    @pyqtSlot()
    def run(self):
        try:
            self.bytes = bytes.fromhex(self.data)
            printCommand.debug(f"Printer Command sent {self.bytes}")
            self.cmd = self.bytes.hex().upper()
            if self.cmd == self.print_setupCode:
                self.serial_obj.write(self.bytes)
                self.response = self.awaitResponse()
                self.setup_printer()

            elif self.cmd.startswith("1B4C"):
                image = self.cmd[4:]
                self.serial_obj.write(self.bytes)
                printCommand.debug(f"Print Image Index {image} command sent ")
                self.signals.response.emit(
                    (f'Print Image Index {image} command sent', " "))
                self.signals.response.emit((f'Waiting for 2 seconds', " "))
                printCommand.debug(f"Waiting for 2 seconds")
                time.sleep(2)
                self.serial_obj.write(bytes.fromhex("FFFF"))
                self.signals.response.emit((f'FF FF Command Sent', " "))
                printCommand.debug(f"FF FF Command Sent")

        except Exception as e:
            error.exception(e)
            self.signals.error.emit((self.data, e))
        self.signals.finished.emit()

    def setup_printer(self):
        printCommand.debug(f"Printer Response {self.response}")
        if isinstance(self.response, bool):
            printCommand.debug(
                f"Printer Response Timed Out for the sent cmd {self.cmd}")
            self.signals.response.emit(
                ('Printer Response Timed Out', f" for the sent cmd {self.cmd}"))
        elif isinstance(self.response, bytes):
            self.response = self.response.hex().upper()
            if self.response == self.response_code:
                printCommand.debug(f"{self.response} Printer Ready!")
                self.signals.response.emit((self.response, " Printer Ready!"))
            else:
                printCommand.debug(f"{self.response} Printer Not Ready!")
                self.signals.response.emit(
                    (self.response, " Printer Not Ready!"))

    def awaitResponse(self, monitor=True):
        try:
            start = time.time()
            while monitor:
                if self.serial_obj.is_open \
                        and self.serial_obj.in_waiting > 0:
                    # meet both conditions
                    buffer = self.serial_obj.read(50)
                    if len(buffer):
                        return buffer
                time.sleep(DELAY)
                if (time.time() - start) > self.timeout:
                    return False
        except Exception as e:
            error.exception(e)
            return e


class WriteWorkerSignals(QObject):
    finished = pyqtSignal(str)
    progress = pyqtSignal(tuple)
    response = pyqtSignal(tuple)
    error = pyqtSignal(tuple)


class WriteWorker(QRunnable):
    def __init__(self, serial_obj, data, timeout=10):
        super().__init__()
        self.serial_obj = serial_obj
        self.data = data
        self.timeout = timeout
        self.signals = WriteWorkerSignals()
        self.chunkResponseCodes = [
            "1C5100", "1C5101",
            "1C5102", "1C5103",
            "1C5104", "1C5105",
            "1C1600"
        ]

    @pyqtSlot()
    def run(self):
        status = ""
        # Check whether Image Chunks
        if isinstance(self.data, list):
            status = self.transfer_logo_file()
        # or Command
        elif isinstance(self.data, str):
            self.send_command()
        self.signals.finished.emit(status)

    def transfer_logo_file(self):
        try:
            idx = 1
            for chunk in self.data:
                fileTransfer.debug(f"Processing Chunk {idx}")
                line = " ".join(chunk)
                _bytes = bytes.fromhex(line)
                sentBytes = self.serial_obj.write(_bytes)
                msg = f"{datetime.now()} : Client==> Chunk : {idx} Sent | {sentBytes} Bytes"
                self.signals.progress.emit((msg, sentBytes))
                self.response = self.awaitResponse()
                fileTransfer.debug(f"Response {self.response} \
                                    received from MCU after the Chunk {idx}")

                if isinstance(self.response, bool):
                    fileTransfer.debug(
                        f"Response Timed Out for {sentBytes} Bytes Sent")
                    msg = f"{datetime.now()} : Device==> Response Timed Out | Chunk {idx} |"
                    self.signals.response.emit((msg, TIMEOUT, 0))
                    return TIMEOUT

                elif isinstance(self.response, bytes):
                    self.response = self.response.hex().upper()
                    fileTransfer.debug(f"Response : {self.response}")

                    if self.response == CHUNK_SUCCESS:
                        msg = f"{datetime.now()} : Device==> Response : SUCCESS"
                        self.signals.response.emit(
                            (msg, CHUNK_SUCCESS, len(chunk)-10))
                        idx = idx+1

                    elif self.response == CHUNK_LOGO_INDEX_UNAVAILABLE:
                        msg = f"{datetime.now()} : Device==> Response : LOGO INDEX UNAVAILABLE"
                        self.signals.response.emit(
                            (msg, CHUNK_LOGO_INDEX_UNAVAILABLE))
                        return FAIL

                    elif self.response == CHUNK_LOGO_EXISTS:
                        msg = f"{datetime.now()} : Device==> Response : LOGO ID ALREADY EXISTS"
                        self.signals.response.emit((msg, CHUNK_LOGO_EXISTS))
                        return FAIL

                    elif self.response == CHUNK_LOGO_INDEX_INVALID:
                        msg = f"{datetime.now()} : Device==> Response : LOGO INVALID INDEX"
                        self.signals.response.emit(
                            (msg, CHUNK_LOGO_INDEX_INVALID))
                        return FAIL

                    elif self.response == CHUNK_LOGO_WRITE_ERROR:
                        msg = f"{datetime.now()} : Device==> Response : LOGO WRITE ERROR"
                        self.signals.response.emit(
                            (msg, CHUNK_LOGO_WRITE_ERROR))
                        return FAIL

                    elif self.response == CHUNK_LOGO_GENERIC_ERROR:
                        msg = f"{datetime.now()} : Device==> Response : LOGO GENERIC ERROR"
                        self.signals.response.emit(
                            (msg, CHUNK_LOGO_GENERIC_ERROR))
                        return FAIL

                else:
                    msg = f"{datetime.now()} : Device==> Response : {self.response} | Aborting Transfer!"
                    self.signals.response.emit((msg, self.response))
                    return FAIL
            return SUCCESS
        except Exception as e:
            error.exception(f"{e}")
            self.signals.error.emit(("", e))

    def send_command(self):
        try:
            _bytes = bytes.fromhex(self.data)
            sentBytes = self.serial_obj.write(_bytes)
            msg = f"{datetime.now()} : Client==> Sent - {self.data}"
            self.signals.progress.emit((msg, sentBytes))
            response = self.awaitResponse()
            sendCommand.debug(f"Sent {_bytes}")
            if isinstance(response, bool):
                sendCommand.debug(f"Response - Timed Out | Sent {self.data}")
                msg = f"{datetime.now()} : Device==> Response - Timed Out | Sent {self.data}"
                self.signals.response.emit((msg, TIMEOUT))
            elif isinstance(response, bytes):
                response = response.hex().upper()
                print(type(response))
                sendCommand.debug(f"Response : {response} | Sent {self.data}")
                msg = f"{datetime.now()} : Device==> Response : {response} | Sent {self.data}"
                self.signals.response.emit((msg, response))
            else:
                sendCommand.debug(f"{response} for {sentBytes} Bytes Sent")
                msg = f"{datetime.now()} : Device==> Error : {response} | Sent {self.data}"
                self.signals.response.emit((msg, response))
        except Exception as e:
            error.exception(f"{e}")
            self.signals.error.emit((self.data, e))

    def awaitResponse(self, monitor=True):
        try:
            start = time.time()
            while monitor:
                if self.serial_obj.is_open \
                        and self.serial_obj.in_waiting > 0:
                    # meet both conditions
                    buffer = self.serial_obj.read(50)
                    if len(buffer):
                        return buffer
                time.sleep(DELAY)
                if (time.time() - start) > self.timeout:
                    return False
        except Exception as e:
            error.exception(e)
            return e


class MainApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self.disable_com_buttons(False)

        self.ui.pushButton_4.clicked.connect(self.refresh_com_port)
        self.ui.pushButton_8.clicked.connect(self.sendCommand)
        self.ui.pushButton_2.clicked.connect(self.connect_to_com_port)
        self.ui.pushButton_3.clicked.connect(self.config_baud_rate)
        self.ui.SelectImage.clicked.connect(
            lambda: self.select_logo('img.log'))
        self.ui.pushButton_5.clicked.connect(self.delete_images)
        self.ui.pushButton_6.clicked.connect(self.print_image)
        self.ui.pushButton.clicked.connect(self.set_image_index_ref)
        self.ui.pushButton_7.clicked.connect(self.bulk_img_transfer)
        self.ui.pushButton_9.clicked.connect(self.close)
        self.ui.pushButton_10.clicked.connect(
            lambda: self.clearConsole(self.ui.listWidget))
        self.ui.pushButton_11.clicked.connect(self.delete_images_from_board)
        self.ui.selectCSV.clicked.connect(
            lambda: self.extractCsvWorker('csv.log'))
        self.ui.isBulkTransferButton.clicked.connect(self.toggleBulkTransfer)

        self.ui.comboBox_2.activated.connect(self.select_port)
        self.ui.comboBox.activated.connect(
            lambda: self.selected_baud_rate(self.ui.comboBox))

        self.ui.label_3.setHidden(True)

        self.ui.groupBox_12.setHidden(True)
        self.ui.pushButton.setHidden(True)
        self.ui.progressBar.setHidden(True)
        self.ui.overAllProgress.setHidden(True)

        self.ui.scrollArea.setWidgetResizable(True)
        self._layout = QtWidgets.QVBoxLayout(self.ui.scrollAreaWidgetContents)

        self.pbar = 0
        self.serial = None
        self.file = None
        self.ref = None
        self.idx = None
        self.printImgIdx = None
        self._height = None
        self._width = None
        self.images = []
        self.bytesArray = []
        self.data_chunks = []
        self.ImagesBytesArray = []
        self.max_width = 576
        self.chunk_size = 896
        self.bulkTransfer = True

        self.threadpool = QThreadPool()
        ################################################################################

        self.set_com_buttons(False)
        self.ui.pushButton_15.clicked.connect(self.refresh_com_port)
        self.ui.pushButton_12.clicked.connect(self.connect_to_com_port)
        self.ui.pushButton_13.clicked.connect(self.config_baud_rate)
        self.ui.pushButton_14.clicked.connect(self.close)
        self.ui.pushButton_16.clicked.connect(
            lambda: self.select_bin_file('bin.log'))
        self.ui.pushButton_17.clicked.connect(self.send_file_worker)
        self.ui.pushButton_19.clicked.connect(
            lambda: self.clear_console(self.ui.listWidget_2))
        self.ui.pushButton_18.clicked.connect(self.send_command_flash)
        self.ui.initiateButton.clicked.connect(self.automate)

        self.ui.comboBox_3.activated.connect(self.select_port)
        self.ui.comboBox_4.activated.connect(
            lambda: self.selected_baud_rate(self.ui.comboBox_4))
        self.ui.pushButton_12.setEnabled(False)
        self.ui.groupBox.setHidden(True)
        # Default for progress bar 0 - 100. Set precision to 2 decimal places. Multiply by 100
        self.ui.progressBar_2.setMaximum(100*100)
        self.ui.progressBar_2.setFormat("%.02f %%" % 0.00)

        self.push_mode = None
        self.selected_port = None
        self.serial = None
        self.file = None
        self.BinFile = None
        self.baudrate = None
        self.timeout = 0
        self.delay = 0.1
        self.chunksize = 8192
        self.ports = []
        self.baudrate_list = []

        self.flashBaudRate = 115200
        self.logoBaudRate = 19200
        self.init = True
        self.ready = False
        self.automateTask = False
        self.isDeleteCMD = False
        self.isLogoCMD = False
        self.isManualCMD = False
        self.firstBaudRateChosen = None
        self.fileTransferFlag = False
        self.imagesTransferFlag = False

        self.fileTransferGif = QMovie(":/gifs/gifs/Blocks-1s-31px.gif")
        self.ui.label.setMovie(self.fileTransferGif)

    #################################################################################

    ####################################################################################
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        '''
        Function to prompt whether the app can be closed
        '''
        close = QMessageBox.question(
            self,
            'QUIT',
            'Are you sure you want to quit?',
            QMessageBox.Yes | QMessageBox.No)
        if close == QMessageBox.Yes:
            try:
                self.serial.close()
                self.threadPool.clear()
            except Exception as e:
                error.exception(e)
            event.accept()
        else:
            event.ignore()

    def clear_console(self, widget):
        '''
        Function to Clear the Console
        :param: widget - QListWidget
        :return: None
        '''
        widget.clear()

    def set_com_buttons(self, state):
        '''
        Enable or Disable all buttons on UI ( except refresh button)
        :param: state - bool
        :return: None
        '''
        self.ui.pushButton_17.setEnabled(state)
        self.ui.pushButton_12.setEnabled(state)
        self.ui.pushButton_18.setEnabled(state)
        self.ui.pushButton_16.setEnabled(state)
        self.ui.pushButton_13.setEnabled(state)
        self.ui.pushButton_14.setEnabled(state)

    def select_bin_file(
            self, file, msg="Select BIN file",
            ext="Binaries (*.bin)"
    ):
        '''
        Select Binary file and convert it into Hex Bytes Array
        :param: file: path to log file( ex bin.log ) to save 
               selected binary file's current directory
        :param: msg: Message display on the File selection Window
        :param: ext: allow only BIN file selection (BIN (*.bin))
        :return: None
        '''
        try:
            if not os.path.exists(file):
                self.BinFile = QFileDialog.getOpenFileName(self, msg, " ", ext)
                with open(file, 'wb') as f:
                    f.write(bytes(os.path.dirname(self.BinFile[0]), 'utf-8'))
            else:
                with open(file, 'rb') as f:
                    dir = f.read().decode('utf-8')
                    if len(dir) > 0:
                        dir = dir
                    else:
                        dir = " "
                self.BinFile = QFileDialog.getOpenFileName(self, msg, dir, ext)
                if len(self.BinFile[0]) > 0:
                    with open(file, 'wb') as f:
                        f.write(bytes(os.path.dirname(
                            self.BinFile[0]), 'utf-8'))
        except Exception as e:
            msg = f"{datetime.now()} : Client==> {e}"
            self.log_event(self.ui.listWidget_2, msg)
            error.exception(e)

        if not len(self.BinFile[0]):
            msg = f"{datetime.now()} : Client==> No file Selected!"
            self.log_event(self.ui.listWidget_2, msg)
            self.ui.pushButton_17.setEnabled(False)
            self.ui.pushButton_18.setEnabled(False)
        else:
            try:
                self.ui.label_4.setText(self.BinFile[0])
                msg = f"{datetime.now()} : Client==> Bin file selected: {self.BinFile[0]}"
                self.log_event(self.ui.listWidget_2, msg)

                self._size = os.path.getsize(self.BinFile[0])
                self.size_hex = bytes.fromhex(
                    "{0:08X}".format(self._size)
                ).hex(" ", 1).upper()
                with open(self.BinFile[0], 'rb') as f:
                    data = f.read()
                    hash = crc8.crc8()
                    hash.update(data)
                    _crc8 = int(hash.hexdigest(), 16)
                    _crc8 = bytes.fromhex(
                        "{0:08X}".format(_crc8)
                    ).hex(" ", 1).upper()
                self.cmd_mode = f"46 57 3E {_crc8} {self.size_hex} 3C 53 54 50"

                self.ui.pushButton_18.setEnabled(True)
            except Exception as e:
                error.exception(e)
                msg = f"{datetime.now()} : Client==> {e}"
                self.log_event(self.ui.listWidget_2, msg)

    def automate(self):
        self.automateTask = not self.automateTask
        self.ui.initiateButton.setText("Automate")
        if (self.automateTask):
            self.ui.initiateButton.setText("Manual")
            self.send_command_flash()

    def refresh_com_port_flash(self):
        '''
        Refresh the COM ports available in the Device
        Enables Connect Button after refresh
        :param: None
        :return: None
        '''
        self.ports = []
        self.ui.comboBox_3.clear()
        self.ports = list(list_ports.comports())
        for port in self.ports:
            self.ui.comboBox_3.addItem(f"{port}")
        if len(self.ports):
            self.selected_port = str(self.ports[0]).split("-")[0].strip()
            self.ui.pushButton_12.setEnabled(True)

    def set_baudrate_on_connected_port(self):
        '''
        Set Baud Rate on the Connected Port
        UI Changes:
            1. Enable Disconnect Button
            2. Disable Set Button
            3. Enable Send Firmeware mode button ( if file is selected)
        :param: None
        :return: None
        '''
        try:
            self.serial.baudrate = int(self.ui.comboBox_4.currentText())
            msg = f"{datetime.now()} : Client==> BaudRate Set to {self.serial.baudrate}"
            self.log_event(self.ui.listWidget_2, msg)
            self.ui.pushButton_13.setEnabled(False)  # baud rate button
            self.ui.pushButton_14.setEnabled(True)  # disconnect button
            self.ui.pushButton_17.setEnabled(False)  # send file
            self.ui.pushButton_16.setEnabled(True)
            if isinstance(self.serial, serial.serialwin32.Serial):
                if self.serial.is_open and self.file is not None:
                    self.ui.pushButton_17.setEnabled(True)
            self.serial.flush()

        except Exception as e:
            msg = f"{datetime.now()} : Client==> Error Setting Baud Rate - {e}"
            self.log_event(self.ui.listWidget_2, msg)
            error.exception(e)

    @pyqtSlot(object)
    def com_error(self, err):
        '''
        Qt function triggered if an exception is raised while trying
        to create Serial Object on a selected port
        :param: err - object - Serial Exception
        :return: None
        '''
        msg = f"{datetime.now()} : Client==> Unable to connect to port {self.selected_port} - Error : {err}"
        self.log_event(self.ui.listWidget_2, msg)

    @pyqtSlot(object)
    def connect_to_com_port_flash(self, serial):
        '''
        Qt function triggered after the serial Object on selected port is ready
        updates available baudrates in combobox
        UI Changes:
            Disables:
                1. Connect Button
            Enables:
                1. Set Button
            Populates:
                1. Baud Rate ComboBox
        :param: serial - object - serial.serialwin32.Serial Object
        :return: None
        '''

        self.serial = serial
        msg = f"{datetime.now()} : Client==> Port {self.selected_port} Ready!"
        self.log_event(self.ui.listWidget_2, msg)
        if self.push_mode is not None:
            self.serial.baudrate = 115200
            msg = f"{datetime.now()} : Client==> BaudRate Set to {self.serial.baudrate}"
            self.log_event(self.ui.listWidget_2, msg)
            self.push_mode = None
            self.ui.pushButton_13.setEnabled(False)  # baud rate button
            self.ui.pushButton_14.setEnabled(True)  # disconnect button
            self.ui.pushButton_17.setEnabled(True)  # send file
            self.ui.pushButton_18.setEnabled(True)
        else:
            self.available_baudrates(self.ui.comboBox_4, self.serial)
            # Update the button status
            self.ui.pushButton_12.setEnabled(False)
            self.ui.pushButton_13.setEnabled(True)

    def available_baudrates(self, comboBox, serial):
        '''
        Populates available Baud Rates for a COM port
        :param: combobox - QComboBox
        :param: serial - serial.serialwin32.Serial Object
        :return: None
        '''
        try:
            comboBox.clear()
            for baud in serial.BAUDRATES:
                comboBox.addItem(f"{baud}")
            self.baudrate = int(comboBox.currentText())
        except Exception as e:
            msg = f"{datetime.now()} : Client==> Unable to get baud Rates - Error : {e}"
            self.log_event(self.ui.listWidget_2, msg)
            error.exception(e)

    def init_com_connection_worker(self):
        '''
        Worker function to create a serial Object from the selected port
        :param: None:
        :return: None:
        Emitted signals trigger call backs
        Signal - serial_available - port connection is ready
        Signal - error - error while setting up port
        '''
        worker = ComWorkerFlash(self.selected_port)
        worker.signals.serial_available.connect(self.connect_to_com_port_flash)
        worker.signals.error.connect(self.com_error)
        self.threadpool.start(worker)

    def close_port(self):
        '''
        Disconnects from the port
        UI changes:
            1. Disable Send Button
            2. Disable Set Button.
            3. Enable refresh button
            4. Disable File browser button
        :param: None
        :return: None
        '''
        try:
            self.serial.close()
        except Exception as e:
            msg = f"{datetime.now()} : Client==> Error Disconnecting {self.selected_port} - Error : {e}"
            self.log_event(self.ui.listWidget_2, msg)
            error.exception(e)
        else:
            msg = f"{datetime.now()} : Client==> Port {self.selected_port} disconnected!"
            self.log_event(self.ui.listWidget_2, msg)
            self.set_com_buttons(False)
            self.ui.groupBox.setHidden(True)
            self.fileTransferGif.stop()
            self.ui.progressBar_2.setValue(0)
            self.ui.pushButton_12.setEnabled(True)

    def update_selected_port(self, index):
        '''
        This function is triggered when the Combo Box index changes.
        Sets the selected port
        :param: index - int 
        :return: None
        '''
        self.selected_port = str(self.ports[index]).split("-")[0].strip()
        msg = f"{datetime.now()} : Client==> Port Selected {self.selected_port}"
        self.log_event(self.ui.listWidget_2, msg)

    def log_event(self, listWidget, msg):
        '''
        Logs the events to the List Widget
        param: listWidget - QListWidget
        :param: msg - str
        :return: None
        '''
        item = QListWidgetItem(msg)
        listWidget.addItem(item)
        listWidget.scrollToItem(item)

    def automate_logo_upload(self):
        self.init = False
        self.fileTransferFlag = True
        self.close()
        self.baudrate = 19200
        self.connect_to_com_port()
        # self.baudrate = 19200
        # self.config_baud_rate()
        # time.sleep(0.2)
        # self.delete_images_from_board()

    @pyqtSlot(tuple)
    def fileResponse(self, data):
        '''
        Qt slot Function triggered when a response is received 
        after the file transfer initiation and during the 
        file transfer
        :param: data - str
        :return: None:
        '''
        self.log_event(self.ui.listWidget_2, data[0])

    @pyqtSlot(tuple)
    def fileFinished(self, msg):
        '''
        Qt slot function triggered after 
        the file transfer operation is complete 
        ( file can be either failure or success)
        UI Changes;
            1. Stops Gif animation
            2. Hide Progress bar and GIF label
            3. Enables send file button
        :param: None:
        :return: None:
        '''
        # msg = f"{datetime.now()} : Client==> Operation Done!"
        self.log_event(self.ui.listWidget_2, msg[0])
        self.ui.groupBox.setHidden(True)
        self.fileTransferGif.stop()
        self.ui.progressBar_2.setValue(0)
        self.ui.pushButton_18.setEnabled(True)
        if msg[1] == SUCCESS:
            # TODO - reconnect to port at 19200
            # self.playSound(file=get_path("./sounds/alert.wav"))
            self.playSound(beep="beep", beepTones=1)
            if self.automateTask:
                self.ui.tabWidget.setCurrentIndex(0)
                self.automate_logo_upload()

        else:
            # TODO - retry
            # if self.automateTask:
            #     pass
            self.playSound(beep="beep", beepTones=3)
            # self.playSound(file=get_path("./sounds/critical.wav"))

    @pyqtSlot(float)
    def fileProgress(self, sent):
        '''
        Qt slot function triggered when the file progress
        percentage value is available during the transfer
        :param: sent - int ( percentage value )
        :return: None
        '''
        self.ui.progressBar_2.setValue(int(sent*100))
        self.ui.progressBar_2.setFormat("%0.2f %%" % sent)

    @pyqtSlot(object)
    def fileError(self, err):
        '''
        Qt slot function triggered when an error is 
        encountered during the file transfer
        :param: err ( Exception Object)
        :return: None
        '''
        msg = f"{datetime.now()} : Client==> {err}"
        self.log_event(self.ui.listWidget_2, msg)
        self.ui.groupBox.setHidden(True)
        self.fileTransferGif.stop()
        self.ui.progressBar_2.setValue(0)
        self.ui.pushButton_18.setEnabled(True)

    def send_file_worker(self):
        '''
        File Worker to send BIN file
        Following Signals when emitted will trigger call backs
        signal - response - responses during or after file transfer
        signal - error - error during file transfer
        signal - finished - file transfer operation complete( fail or success)
        UI Changes:
            1. Enable Progress bar and loading GIF
            2. Disable file transfer button
        :param: None
        :return: None
        '''
        try:
            worker = fileTransferWorker(self.serial, self.BinFile[0],
                                        self.delay, self.timeout,
                                        self.chunksize)
            worker.signals.response.connect(self.fileResponse)
            worker.signals.error.connect(self.fileError)
            worker.signals.finished.connect(self.fileFinished)
            worker.signals.progress.connect(self.fileProgress)
            self.threadpool.start(worker)
            self.ui.groupBox.setHidden(False)
            self.fileTransferGif.start()
            self.ui.pushButton_17.setEnabled(False)
        except Exception as e:
            error.exception(e)

    @pyqtSlot(tuple)
    def push_command_response(self, data):
        '''
        Qt slot function triggered when a
        response is received after init command
        :param: response - str
        :return: None
        '''
        response, cmd = data
        msg = f"{datetime.now()} : Device==> {response}"
        self.log_event(self.ui.listWidget_2, msg)
        if cmd == CMD and response == READY:
            self.ui.pushButton_17.setEnabled(True)
            self.ui.pushButton_18.setEnabled(False)
            if self.automateTask:
                time.sleep(1)
                self.send_file_worker()

        # elif response == TIMEOUT:
        #     self.ui.pushButton_17.setEnabled(False)
        #     self.ui.pushButton_18.setEnabled(False)
        #     if self.automateTask:
        #         time.sleep(0.3)
        #         self.send_command_flash()
        elif cmd == CMD and response != READY:
            self.ui.pushButton_17.setEnabled(False)
            self.ui.pushButton_18.setEnabled(False)

            self.automateTask = False
            self.ui.initiateButton.setText("Automate")
            self.playSound(beep="beep", beepTones=3)
            self.playSound(file=get_path("./sounds/critical.wav"))

    @pyqtSlot(tuple)
    def push_command_error(self, err):
        '''
        Qt slot function triggered when an error is 
        encountered during the init command
        :param: err ( Exception Object )
        :return: None
        '''
        msg = f"{datetime.now()} : Device==> {err}"
        self.log_event(self.ui.listWidget_2, msg)

    @pyqtSlot()
    def push_command_finished(self):
        '''
        Qt slot function triggered when an init command
        operation is complete
        :param: None
        :return: None
        '''
        msg = f"{datetime.now()} : Client==> Operation done!"
        self.log_event(self.ui.listWidget_2, msg)
        self.ui.pushButton_18.setEnabled(True)

    @pyqtSlot(object)
    def push_command_new_serial(self, ser):
        '''
        Qt slot function triggered when a new
        Serial Object is available
        :param: ser - Serial Object
        :return: None
        '''
        self.serial = ser
        msg = f"{datetime.now()} : Client==> Reconnected Port {self.selectedPort} Ready!"
        self.log_event(self.ui.listWidget_2, msg)

    def send_command_flash(self):
        '''
        Helper Worker function to push Command to ready MCU
        Following Signals when emitted will trigger call backs
        signal - response - responses during or after init command
        signal - error - error during init command
        signal - serial - new Serial Object After re-connecting 
                to a port with 115200 baudrate
        signal - finished - end of the init command
        UI Changes:
            1. Enable Progress bar and loading GIF
            2. Disable file transfer button
        :param: None
        :return: None
        '''
        try:
            self.ui.pushButton_18.setEnabled(False)
            worker = pushCommand(self.serial, self.cmd_mode,
                                 self.selectedPort
                                 )
            worker.signals.error.connect(self.push_command_error)
            worker.signals.finished.connect(self.push_command_finished)
            worker.signals.serial.connect(self.push_command_new_serial)
            worker.signals.response.connect(self.push_command_response)
            self.threadpool.start(worker)

        except Exception as e:
            msg = f"{datetime.now()} : Client==> {e}"
            self.log_event(self.ui.listWidget_2, msg)
            error.exception(e)

    #########################################################################################

    def showScrollAreaButtons(self):
        if self.ui.SelectImage.isVisible() and len(self.ImagesBytesArray) > 0:
            self.update_buttons_on_scrollarea()
            self.ui.pushButton_7.setEnabled(False)

    def deleteButtonsFromScrollArea(self):
        for i in range(self._layout.count()):
            if self._layout.itemAt(i) is not None:
                widget = self._layout.itemAt(i).widget()
                if isinstance(widget, PyQt5.QtWidgets.QPushButton):
                    self._layout.removeWidget(widget)
                    widget.deleteLater()

    def toggleBulkTransfer(self):
        if self.bulkTransfer:
            self.bulkTransfer = False
            self.ui.isBulkTransferButton.setText("Enable Bulk Transfer")
            self.ui.SelectImage.setHidden(False)
            self.ui.selectCSV.setHidden(True)
            self.ui.groupBox_12.setHidden(False)
            self.ui.pushButton.setHidden(False)
            self.showScrollAreaButtons()
        else:
            self.bulkTransfer = True
            self.ui.isBulkTransferButton.setText("Disable Bulk Transfer")
            self.ui.SelectImage.setHidden(True)
            self.ui.selectCSV.setHidden(False)
            self.ui.groupBox_12.setHidden(True)
            self.ui.pushButton.setHidden(True)
            self.deleteButtonsFromScrollArea()
            if len(self.ImagesBytesArray) > 0:
                self.ui.pushButton_7.setEnabled(True)

    def extractCsvWorker(self, prev_location):
        '''
        Worker function to extract Img Indexes
        And Reference
        param: None:
        return: None:
        '''
        msg = "Select the CSV file"
        ext = "CSV files (*.csv)"

        try:
            if not os.path.exists(prev_location):
                file = QFileDialog.getOpenFileName(self, msg, " ", ext)
                with open(prev_location, 'wb') as f:
                    f.write(bytes(os.path.dirname(self.file[0]), 'utf-8'))
            else:
                with open(prev_location, 'rb') as f:
                    dir = f.read().decode('utf-8')
                    if len(dir) > 0:
                        dir = dir
                    else:
                        dir = " "
                file = QFileDialog.getOpenFileName(self, msg, dir, ext)

                if len(file[0]) > 0:
                    with open(prev_location, 'wb') as f:
                        f.write(bytes(os.path.dirname(file[0]), 'utf-8'))

        except Exception as e:
            msg = f"{datetime.now()} : Client==> {e}"
            self.logEvent(self.ui.listWidget, msg)
            error.exception(e)

        if not len(file[0]):
            msg = f"{datetime.now()} : Client==> No CSV file Selected!"
            self.logEvent(self.ui.listWidget, msg)
        else:

            self.delete_images()
            worker = CsvExtractorWorker(file[0])
            worker.signals.lines.connect(self.receive_csv_img_index_reference)
            worker.signals.error.connect(self.csvError)
            self.threadpool.start(worker)

    def refresh_com_port(self):
        '''
        Refresh Available Com ports on computer
        Populates the ComboBox with COM ports
        Enables Connect Button 
        If one or more COM ports available
        return: None 
        '''
        self.ports = []
        self.ui.comboBox_2.clear()
        self.ui.comboBox_3.clear()
        self.ports = list(list_ports.comports())
        for port in self.ports:
            self.ui.comboBox_2.addItem(f"{port}")
            self.ui.comboBox_3.addItem(f"{port}")
        if len(self.ports):
            self.selectedPort = str(self.ports[0]).split("-")[0].strip()
            self.ui.pushButton_2.setEnabled(True)
            self.ui.pushButton_12.setEnabled(True)

    def disable_com_buttons(self, state):
        '''
        Function to Disable All buttons
        '''
        self.ui.SelectImage.setEnabled(state)
        self.ui.pushButton_5.setEnabled(state)
        self.ui.pushButton_11.setEnabled(state)
        self.ui.pushButton_7.setEnabled(state)
        self.ui.pushButton.setEnabled(state)
        self.ui.pushButton_3.setEnabled(state)
        self.ui.pushButton_8.setEnabled(state)
        self.ui.pushButton_9.setEnabled(state)
        self.ui.pushButton_10.setEnabled(state)
        self.ui.pushButton_6.setEnabled(state)
        self.ui.pushButton_2.setEnabled(state)
        self.ui.selectCSV.setEnabled(state)
        self.ui.isBulkTransferButton.setEnabled(state)
        self.ui.lineEdit.setEnabled(state)
        self.ui.lineEdit_2.setEnabled(state)
        self.ui.lineEdit_3.setEnabled(state)

    def set_cmd_buttons(self, state):
        '''
        Function to enable & disable following buttons
        1. Push Command
        2. Delete Image from board
        3. Print Image button
        4. Load Image button
        This is to prevent other operations when
        another operation is already ongoing
        '''
        self.ui.pushButton_6.setEnabled(state)
        self.ui.pushButton_7.setEnabled(state)
        self.ui.pushButton_8.setEnabled(state)
        self.ui.pushButton_11.setEnabled(state)

    def set_file_associated_buttons(self, state):
        '''
        Function to Enable & Disable following buttons
        1. Image Delete Button
        2. Set Index button
        3. line edit - Img index
        4. line edit - Img ref
        '''
        self.ui.pushButton_5.setEnabled(state)  # img delete button
        self.ui.pushButton.setEnabled(state)  # set index
        self.ui.lineEdit_2.setEnabled(state)
        self.ui.lineEdit_3.setEnabled(state)

    def print_image(self):
        '''
        Initiates Print operation after
        Validating the Img Index
        Img Index - Only one Character Allowed
        Disables all other commands until print is complete
        Button disabled:
        1. Push command
        2. Load Image to board
        3. Delete images from board
        return: None
        '''
        self.printCMD = "FF FF 81"
        self.printImgIdx = self.ui.lineEdit_4.text()
        if len(self.printImgIdx) > 0:
            _index = self.validate_img_index(
                self.printImgIdx, self.ui.pushButton_6)
            if isinstance(_index, Exception):
                title = "Invalid Print Image Index"
                icon = QMessageBox.Critical
                buttons = QMessageBox.Ok
                self.dialog(title, _index, icon, buttons)
                self.ui.pushButton_6.setEnabled(True)
                self.ui.lineEdit_4.setText("")
                self.printImgIdx = None
            elif isinstance(_index, str):
                self.ui.lineEdit_4.setText("")
                title = "Invalid Print Image Index"
                icon = QMessageBox.Critical
                buttons = QMessageBox.Ok
                self.dialog(title, _index, icon, buttons)
                self.ui.pushButton_6.setEnabled(True)
                self.printImgIdx = None
            else:
                msg = f"{datetime.now()} : Client==> {self.printCMD}"
                self.logEvent(self.ui.listWidget, msg)
                self.send_print_command()
                self.set_cmd_buttons(False)
        else:
            msg = f"{datetime.now()} : Client==> Invalid Input: Print Image Index Field Cannot be blank"
            self.logEvent(self.ui.listWidget, msg)
            title = "Invalid Input"
            text = "Print Image Index Field Empty"
            icon = QMessageBox.Warning
            buttons = QMessageBox.Ok
            self.dialog(title, text, icon, buttons)
            self.printImgIdx = None

    def dialog(self, title, text, icon, buttons):
        '''
        Function to Display QMessage Box
        param: title: Message Box title
        param: text: Message Box body
        param: icon: warning or critical or info
        param: buttons: standard buttons[ OK or (yes and no) ]
        return: None
        '''
        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setText(text)
        dlg.setStandardButtons(buttons)
        dlg.setIcon(icon)
        dialogue = dlg.exec()
        return dialogue

    def validate_img_index(self, index, button):
        '''
        Validates the Image Index Input
        Img Index - Only 1 Character Allowed
        Enables and Disables Index or Print Button
        Based on whether the Index is valid or not.
        Displays Warning & Error about invalid Input
        :param: index - str - selected img index input
        :param: button - QPushButton Object 
        :return: None or str or Exception
        '''
        text = ""
        if not index.isalpha() or not index.isupper() or len(index) > 1:
            msg = f"{datetime.now()} : Client==> Invalid Input: Only ONE alphabet [A-Z] allowed for Image Index"
            self.logEvent(self.ui.listWidget, msg)
            text += "Only ONE alphabet [A-Z] allowed for Image Index\n"
            button.setEnabled(False)
            return text
        else:
            try:
                index = "{0:02X}".format(ord(index))
                msg = f"{datetime.now()} : Client==> Selected Index {index}"
                self.logEvent(self.ui.listWidget, msg)
                return None

            except Exception as e:
                error.exception(e)
                msg = f"{datetime.now()} : Client==> Invalid Input : {e}"
                self.logEvent(self.ui.listWidget, msg)
                title = "Image Index Error"
                text = str(e)
                icon = QMessageBox.Critical
                buttons = QMessageBox.Ok
                self.dialog(title, text, icon, buttons)
                button.setEnabled(False)
                return e

    def validate_img_ref(self, ref, button):
        '''
        Validates the Image Ref Input
        Img Ref - Only Decimals Allowed - 2 digits
        Enables and Disables Set Index or Print Button
        Based on whether the Ref is valid or not
        Displays Warning & Error about invalid Inputs
        :param: ref - str - selected img ref input
        :param: button - QPushButton Object 
        :return: None or str or Exception
        '''
        text = ""
        if not ref.isnumeric() or len(ref) > 2:
            msg = f"{datetime.now()} : Client==> Invalid Input: \
                                    Only an Integer[ length 2 or 1] allowed for Image Ref"
            self.logEvent(self.ui.listWidget, msg)
            text += "Only an Integer of Length 2 or 1 allowed for Image Ref\n"
            button.setEnabled(False)
            return text
        else:
            try:
                ref = "{0:02X}".format(int(ref))
                msg = f"{datetime.now()} : Client==> Selected Ref {ref}"
                self.logEvent(self.ui.listWidget, msg)
                return None
            except Exception as e:
                error.exception(e)
                msg = f"{datetime.now()} : Client==> Invalid Input : Only Decimals Accepted"
                self.logEvent(self.ui.listWidget, msg)
                title = "Image Reference Error"
                text = "Invalid Input. Only Decimals Accepted"
                icon = QMessageBox.Critical
                buttons = QMessageBox.Ok
                self.dialog(title, text, icon, buttons)
                button.setEnabled(False)
                return e

    def set_image_index_ref(self, img_idx_ref=None):
        '''
        Validates Img Index & Img Ref
        Sets the Img Ref & Index to Selected Image in Scroll Area
        Clears Invalid Inputs
        param: img_idx_ref: tuple(idx,ref): 
        return: bool
        '''
        if not img_idx_ref:
            self.idx = self.ui.lineEdit_3.text()
            self.ref = self.ui.lineEdit_2.text()
        else:
            self.idx = img_idx_ref[0]
            self.ref = img_idx_ref[1]
        if len(self.idx) > 0 and len(self.ref) > 0:
            text = ""
            _index = self.validate_img_index(self.idx, self.ui.pushButton_7)
            _ref = self.validate_img_ref(self.ref, self.ui.pushButton_7)
            if isinstance(_index, Exception) or isinstance(_ref, Exception):
                text += str(_index)
                text += str(_ref)
                title = "Invalid Input"
                icon = QMessageBox.Critical
                buttons = QMessageBox.Ok
                self.dialog(title, text, icon, buttons)
                self.ui.pushButton_7.setEnabled(False)
                self.idx = None
                self.ui.lineEdit_3.setText("")
                self.ref = None
                self.ui.lineEdit_2.setText("")
                return False
            elif isinstance(_index, str) or isinstance(_ref, str):
                if _index is not None:
                    text += _index
                if _ref is not None:
                    text += _ref
                self.ui.pushButton_7.setEnabled(False)
                title = "Invalid Input"
                icon = QMessageBox.Critical
                buttons = QMessageBox.Ok
                self.dialog(title, text, icon, buttons)
                self.ui.pushButton_7.setEnabled(False)
                self.idx = None
                self.ui.lineEdit_3.setText("")
                self.ref = None
                self.ui.lineEdit_2.setText("")
                return False
            else:
                self.idx = "{0:02X}".format(ord(self.idx))
                self.ref = "{0:02X}".format(int(self.ref))
                if len(self.bytesArray) > 0 \
                        and self.idx \
                        is not None and \
                        self.ref is not None:

                    self.ui.pushButton_7.setEnabled(True)
                return True

        else:
            msg = f"{datetime.now()} : Client==> Invalid Input: One or both fields are empty"
            self.logEvent(self.ui.listWidget, msg)
            title = "Invalid Input"
            text = "One or both fields are empty"
            icon = QMessageBox.Warning
            buttons = QMessageBox.Ok
            self.dialog(title, text, icon, buttons)
            self.idx = None
            self.ui.lineEdit_3.setText("")
            self.ref = None
            self.ui.lineEdit_2.setText("")
            self.ui.pushButton_7.setEnabled(False)
            return False

    def select_logo(
            self, prev_location, msg="Select BMP Logo file",
            ext="Images (*.bmp)"
    ):
        '''
        Select BMP logo file and convert it into Hex Bytes Array
        param: file: path to log file( ex img.log ) to save 
               selected image's current directory
        param: msg: Message display on the File selection Window
        param: ext: allow only BMP fil selection (Images (*.bmp))
        return: None
        '''
        try:
            if not os.path.exists(prev_location):
                self.file = QFileDialog.getOpenFileNames(self, msg, " ", ext)
                with open(prev_location, 'wb') as f:
                    f.write(bytes(os.path.dirname(self.file[0][0]), 'utf-8'))
            else:
                with open(prev_location, 'rb') as f:
                    dir = f.read().decode('utf-8')
                    if len(dir) > 0:
                        dir = dir
                    else:
                        dir = " "
                self.file = QFileDialog.getOpenFileNames(self, msg, dir, ext)
                if len(self.file[0][0]) > 0:
                    with open(prev_location, 'wb') as f:
                        f.write(bytes(os.path.dirname(
                            self.file[0][0]), 'utf-8'))
        except Exception as e:
            msg = f"{datetime.now()} : Client==> {e}"
            self.logEvent(self.ui.listWidget, msg)
            error.exception(e)

        _list = list(self.file)
        _list.append(prev_location)
        self.file = tuple(_list)

        if not len(self.file[0]):
            msg = f"{datetime.now()} : Client==> No file Selected!"
            self.logEvent(self.ui.listWidget, msg)
            self.set_file_associated_buttons(False)
        else:
            # self.delete_images()
            self.set_file_associated_buttons(True)
            for imgPath in self.file[0]:
                self.convertImage(imgPath)

    def bulk_img_transfer(self):
        # for image,idx_ref in self.ImagesBytesArray:
        #     self._height = image[1]
        #     self.msb_len = "{0:04X}".format(self._height)
        #     self.bytesArray = image[0]
        #     self.idx = idx_ref[0]
        #     self.ref = idx_ref[1]
        #     self.prepare_data_chunks()
        self.logoFailedCount = 0
        self.logoTransferCount = 0
        if self.ui.SelectImage.isVisible():
            self.prepare_data_chunks()
        elif len(self.ImagesBytesArray) > 0:
            self.set_file_associated_buttons(False)
            self.bulkWorker = bulkTransferWorker(self.ImagesBytesArray)
            self.bulkWorker.signals.imgStart.connect(self.sendImage)
            self.bulkWorker.signals.progress.connect(self.overAllProgress)
            self.bulkWorker.signals.finished.connect(self.ImagesTransferDone)
            self.threadpool.start(self.bulkWorker)
            self.ui.label_3.setText(
                f"Transferred 0 of {len(self.ImagesBytesArray)}")
            self.ui.overAllProgress.setHidden(False)
            self.ui.label_3.setHidden(False)

    def playSound(self, beep=None, file=None, beepTones=None):
        if beep:
            worker = WindowSoundWorker(beep=beep, beepTones=beepTones)
            self.threadpool.start(worker)
        elif file:
            worker = WindowSoundWorker(file=file)
            self.threadpool.start(worker)

    @pyqtSlot(tuple)
    def overAllProgress(self, image):
        '''
        Qt slot function triggered when an image is transferred
        param: image: tuple(image Count,overall progress,total Images)
        return: None:
        '''
        self.ui.overAllProgress.setValue(image[1])
        self.ui.label_3.setText(f"Transferred {image[0]} of {image[2]}")

    @pyqtSlot(str)
    def ImagesTransferDone(self, success):
        self.ui.pushButton_7.setHidden(False)
        if self.bulkTransfer:
            if isinstance(self.serial, serial.serialwin32.Serial) \
                    and self.serial.is_open \
                    and len(self.ImagesBytesArray) > 0:
                self.ui.pushButton_7.setEnabled(True)
            else:
                self.ui.pushButton_7.setEnabled(False)
        else:
            if isinstance(self.serial, serial.serialwin32.Serial) \
                    and self.file is not None \
                    and self.serial.is_open \
                    and len(self.bytesArray) > 0 \
                    and self.idx is not None \
                    and self.ref is not None:
                self.ui.pushButton_7.setEnabled(True)  # image(s) load button
            else:
                self.ui.pushButton_7.setEnabled(False)
        self.ui.overAllProgress.setValue(0)
        self.ui.overAllProgress.setHidden(True)
        self.ui.label_2.setHidden(True)
        self.ui.label_3.setHidden(True)
        msg = f"{datetime.now()} : Client==> Images Transfer {success}"
        self.logEvent(self.ui.listWidget, msg)
        self.set_file_associated_buttons(True)
        if success == SUCCESS:
            # self.playSound(get_path("./sounds/alert.wav"))
            self.playSound(beep="beep", beepTones=1)
            if self.automateTask:
                self.init = True
                self.imagesTransferFlag = True
                self.close()
                self.baudrate = self.firstBaudRateChosen
                self.connect_to_com_port()
                self.ui.tabWidget.setCurrentIndex(1)
                text = "Do you want to Continue?"
                title = "Input Required!"
                icon = QMessageBox.Question
                buttons_options = QMessageBox.Yes | QMessageBox.No
                dialogue = self.dialog(title, text, icon, buttons_options)
                if dialogue == QMessageBox.Yes:
                    self.send_command_flash()
                else:
                    self.automateTask = False
                    self.ui.initiateButton.setText("Automate")
        else:
            self.playSound(beep="beep", beepTones=3)
            # self.playSound(get_path("./sounds/critical.wav"))
            # text = f"Image(s) Transfer {success}"
            # title = "Failed!"
            # icon = QMessageBox.Critical
            # buttons = QMessageBox.Ok
            # self.dialog(title,text,icon,buttons)

    @pyqtSlot(tuple)
    def sendImage(self, ImageInfo):
        '''
        Qt Slot Function triggered when a new Image is ready for transfer.
        param: ImageInfo: tuple (image,idx_ref)
        image: tuple(BytesArray,Height,Width,Qt_image_obj,filePath)
        idx_ref: list[idx,ref]
        return: None
        '''
        height = ImageInfo[0][1]
        self.msb_len = "{0:04X}".format(height)
        self.idx = ImageInfo[1][0]
        self.ref = ImageInfo[1][1]
        self.bytesArray = ImageInfo[0][0]
        self.prepare_data_chunks()

    def prepare_data_chunks(self):
        '''
        Splits the Hex Bytes Array into 896 bytes data chunk
        Each Data chunk is appended to 10 bytes of MCU command
        First 10 Bytes of each Chunk is as follows:
        '1C 50 img_ref img_index img_height data_chunk_size offset' 
        data_chunk_size is calculated for each chunk
        offset starts with 00 for first chunk and adds 896 for every 
        subsequent chunk. 
        Calculates the final chunk data to be sent along with 10 bytes
        Disables following buttons:
            1. Delete Images from Board
            2. Push Command
            3. Print Command
            4. Load Images to board

        param: None:
        return: None:
        '''
        self.isLogoCMD = True
        self.data_chunks = []
        start = 0
        chunks = 0
        for offset in range(0, len(self.bytesArray), self.chunk_size):
            end = offset + self.chunk_size
            chunks += 1
            if end > len(self.bytesArray):
                remaining = len(self.bytesArray) % self.chunk_size
                data_chunk_size = "{0:04X}".format(remaining)
                end = offset + remaining
            else:
                data_chunk_size = "{0:04X}".format(self.chunk_size)
            chunk_offset = "{0:04X}".format(start)

            cmd = [
                "1C", "50", self.ref,
                self.idx, self.msb_len,
                data_chunk_size,
                chunk_offset
            ]
            cmd.extend(self.bytesArray[start:end])
            self.data_chunks.append(cmd)
            start = end
        msg = f"{datetime.now()} : Client==> Total Chunks : {chunks}"
        self.logEvent(self.ui.listWidget, msg)

        self.ui.progressBar.setHidden(False)
        self.set_cmd_buttons(False)
        self.send_logo_to_board()

    def delete_images(self):
        '''
        Function to clear the Image(s) from ScrollArea
        Reset's the BytesArray
        Disables following Buttons - 
        Delete, Set Index, and Load Image to Board 
        '''
        self.bytesArray = []
        self.ImagesBytesArray = []
        if not self.ui.SelectImage.isVisible():
            self.file = None
        self.idx = None
        self.ref = None
        self.ui.lineEdit_2.setText("")
        self.ui.lineEdit_3.setText("")

        for i in reversed(range(self._layout.count())):
            self._layout.takeAt(i).widget().deleteLater()

        self.set_file_associated_buttons(False)
        self.ui.pushButton_7.setEnabled(False)
        msg = f"{datetime.now()} : Client==> All Image(s) deleted!"
        self.logEvent(self.ui.listWidget, msg)

    def selected_baud_rate(self, comboBox):
        '''
        Updates Selected Baud Rate from the Combox
        '''
        try:
            self.baudrate = int(comboBox.currentText())
            self.firstBaudRateChosen = self.baudrate
        except Exception as e:
            error.exception(e)
            msg = f"{datetime.now()} : Client==> Error setting Baud Rate {e}"
            self.logEvent(self.ui.listWidget, msg)
            self.logEvent(self.ui.listWidget_2, msg)

    def image_added_for_loading(self, image):
        '''
        Tracks the Button click ( image selection)
        And Updates the Button status
        of all the added images into ScrollArea
        '''
        self.bytesArray = image['bytesArray']
        self.msb_len = image['height']
        if len(self.ImagesBytesArray) > 0:
            for imageTuple, idx_ref in self.ImagesBytesArray:
                if image["file"] in imageTuple[-1]:
                    self.idx = idx_ref[0]
                    self.ref = idx_ref[1]
                    self.ui.lineEdit_3.setText(self.idx)
                    self.ui.lineEdit_2.setText(self.ref)
                    break

        msg = f"{datetime.now()} : Client==> Selected Image: {image['file']} Height: {image['height']} (MSB)"
        self.logEvent(self.ui.listWidget, msg)
        if self.ref is not None and self.idx is not None and len(self.bytesArray) > 0:
            self.ui.pushButton_7.setEnabled(True)
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i).widget()
            if isinstance(item, PyQt5.QtWidgets.QPushButton):
                if item.isChecked():
                    if item.pos() == image['button_pos']:
                        pass
                    else:
                        item.setChecked(False)

    def createButton(self, data, insert=False, i=None):
        height = data[1]
        file = data[4]
        button = QtWidgets.QPushButton(self.ui.scrollAreaWidgetContents)
        button.setText(f"Load {os.path.basename(file)}")
        button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        button.setMinimumSize(0, 0)
        button.setMaximumSize(16777215, 16777215)
        button.setCheckable(True)
        if insert:
            self._layout.insertWidget(i+1, button)
        else:
            self._layout.addWidget(button)
        height_msb = "{0:04X}".format(height)
        button.clicked.connect(
            lambda checked, data=data, button=button, file=file:
            self.image_added_for_loading(
                {
                    "bytesArray": data[0],
                    "button_pos": button.pos(),
                    'button': button,
                    'file': os.path.basename(file),
                    'height': height_msb
                }
            )
        )

        button.clicked.connect(lambda: button.setChecked(True))

    def createLabel(self, data):
        qt_img = data[3]
        pixel_data = QPixmap.fromImage(qt_img)
        label = QtWidgets.QLabel(self.ui.scrollAreaWidgetContents)
        label.setPixmap(pixel_data)
        label.setStyleSheet("border: 2px solid rgba(0,98,163,0.5)")
        label.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        label.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self._layout.setSpacing(2)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(label)

    def update_buttons_on_scrollarea(self):
        imgIdx = 0
        i = 0
        while i < self._layout.count():
            item = self._layout.itemAt(i)
            if item is not None:
                widget = item.widget()
                if isinstance(widget, PyQt5.QtWidgets.QLabel):
                    data = self.ImagesBytesArray[imgIdx][0]
                    self.createButton(data, insert=True, i=i)
                    imgIdx += 1
            i += 1

    def display_images_on_scrollarea(self, data):
        '''
        Function to Display added Image using references to 
        their corresponding Qt_image object, and track image 
        button clicks( on scrollarea) to load them onto the board
        :param: data - tuple (BytesArray,Height,Width,Qt_image_obj,filePath)
        :param: file - str - logo image file path
        :return: None
        '''
        self.createLabel(data)

        if not self.ui.SelectImage.isVisible():
            self.convertWorker.wake()
        else:
            self.createButton(data)

    @pyqtSlot(object)
    def receive_csv_img_index_reference(self, images):
        self.convertWorker = ImageConvertWorker(images)
        self.convertWorker.signals.progress.connect(self.imageConverted)
        self.convertWorker.signals.finished.connect(self.convertComplete)
        self.threadpool.start(self.convertWorker)

    @pyqtSlot(str, list)
    def imageConverted(self, imgPath, idx_ref):
        if self.set_image_index_ref(idx_ref):
            self.convertImage(imgPath)
        else:
            self.convertWorker.wake()

    @pyqtSlot()
    def convertComplete(self):
        if len(self.ImagesBytesArray) > 0:
            self.ui.pushButton_7.setEnabled(True)
            self.ui.pushButton_5.setEnabled(True)
            msg = f"{datetime.now()} : Client==> All Images Added!"
        else:
            msg = f"{datetime.now()} : Client==> No Images Added!"
            self.ui.pushButton_7.setEnabled(False)
            self.ui.pushButton_5.setEnabled(False)
        self.logEvent(self.ui.listWidget, msg)

    @pyqtSlot(object)
    def csvError(self, error):
        msg = f"{datetime.now()} : Client==> Error - {error}"
        self.logEvent(self.ui.listWidget, msg)

    @pyqtSlot(list)
    def logo_convert_error(self, err):
        '''
        Qt Slot Function triggered when the Logo IMG conversion fails
        param: err: list[Exception Object or bool, filePath]
        return: None
        '''
        if isinstance(err[0], bool):
            msg = f"{datetime.now()} : Client==> Image {os.path.basename(err[1])} Width Exceeds {self.max_width}"
            self.logEvent(self.ui.listWidget, msg)
            title = "Image Warning"
            text = f"Image {os.path.basename(err[1])} width exceeds {self.max_width}. It was not added!"
            icon = QMessageBox.Warning
            buttons = QMessageBox.Ok
            self.dialog(title, text, icon, buttons)
        else:
            msg = f"{datetime.now()} : Client==> Error Processing Image {err[1]} {err}"
            self.logEvent(self.ui.listWidget, msg)

    @pyqtSlot(bool)
    def logo_invalid(self, img):
        '''
        Qt slot function triggered when the Logo IMG size exceed allowed width
        param: img: bool
        '''

    @pyqtSlot(tuple)
    def render_image_on_ui(self, data):
        '''
        Qt Slot Function triggered when the Logo IMG is converted
        This function is tasked to display each selected Image in Scroll Area.
        param: data: tuple - (BytesArray,Height,Width,Qt_image_obj,filePath)
        return: None
        '''
        self.ImagesBytesArray.append((data, [self.idx, self.ref]))
        self._height = data[1]
        self._width = data[2]
        self.msb_len = "{0:04X}".format(self._height)
        self.lsb_len = "".join([self.msb_len[2:4], self.msb_len[0:2]])
        msg = f"{datetime.now()} : Client==> Image Height - MSB: {self.msb_len} LSB: {self.lsb_len}"
        self.logEvent(self.ui.listWidget, msg)
        self.display_images_on_scrollarea(data)

    def convertImage(self, filePath):
        '''
        Worker Function to convert select Logo to Hex Bytes
        param: filePath: str - Path of the Logo File
        return: None
        '''
        worker = LogoCreatorWorker(filePath, self.max_width)
        worker.signals.dataReady.connect(self.render_image_on_ui)
        worker.signals.error.connect(self.logo_convert_error)
        self.threadpool.start(worker)

    def updateComButtonsOnConnect(self, state):
        # DISABLE Buttons
        # Image Load and Print tab
        self.ui.pushButton_4.setEnabled(not state)  # refresh
        self.ui.pushButton_2.setEnabled(not state)  # connect
        # Flash Tab
        self.ui.pushButton_15.setEnabled(not state)  # refresh
        self.ui.pushButton_12.setEnabled(not state)  # connect

        # ENABLE Buttons
        # Image Load and Print Tab
        self.ui.pushButton_6.setEnabled(state)   # Print image
        self.ui.pushButton_11.setEnabled(state)  # delete board image(s)
        self.ui.pushButton_8.setEnabled(state)   # push command
        self.ui.pushButton_9.setEnabled(state)   # disconnect button
        self.ui.selectCSV.setEnabled(state)      # csv button
        self.ui.SelectImage.setEnabled(state)    # select image
        self.ui.pushButton_10.setEnabled(state)  # clear console
        self.ui.pushButton_3.setEnabled(state)   # set button
        self.ui.isBulkTransferButton.setEnabled(state)
        # bulk transfer toggle
        self.ui.lineEdit.setEnabled(state)       # push command text field
        # Flash Tab
        self.ui.pushButton_13.setEnabled(state)  # set button
        self.ui.pushButton_19.setEnabled(state)  # clear console
        self.ui.pushButton_14.setEnabled(state)  # disconnect button
        self.ui.pushButton_16.setEnabled(state)  # bin file selection

    def updateComButtonsOnDisconnect(self, state):
        # disable:
        # ImageLoad and Print Tab
        self.ui.pushButton_11.setEnabled(state)  # delete board image(s)
        self.ui.pushButton_7.setEnabled(state)   # load image(s)
        self.ui.pushButton_6.setEnabled(state)   # print image
        self.ui.pushButton_8.setEnabled(state)   # push Command
        self.ui.pushButton_3.setEnabled(state)   # Set Baud Rate
        self.ui.pushButton_2.setEnabled(state)   # connect
        self.ui.pushButton_9.setEnabled(state)   # disconnect
        self.ui.selectCSV.setEnabled(state)      # csv button
        self.ui.pushButton_10.setEnabled(state)  # clear console
        self.ui.lineEdit.setEnabled(state)       # push command text field
        # Flash Tab
        self.ui.pushButton_17.setEnabled(state)  # Send bin file
        self.ui.pushButton_18.setEnabled(state)  # firmware update mode
        self.ui.pushButton_13.setEnabled(state)  # set baud rate
        self.ui.pushButton_12.setEnabled(state)  # connect
        self.ui.pushButton_14.setEnabled(state)  # disconnect
        self.ui.pushButton_16.setEnabled(state)  # bin file selection
        self.ui.pushButton_19.setEnabled(state)  # clear console

        # enable:
        # ImageLoad and Print tab
        self.ui.pushButton_4.setEnabled(not state)
        # refresh
        # Flash Tab
        self.ui.pushButton_15.setEnabled(not state)
        # refresh
        # overall images Progress label
        self.ui.label_3.setHidden(True)
        self.ui.label_2.setHidden(True)          # image progress label
        self.ui.progressBar.setHidden(True)      # image progressbar
        self.ui.progressBar.setValue(0)
        self.ui.overAllProgress.setHidden(True)  # overall images progresssbar
        self.ui.overAllProgress.setValue(0)

    @pyqtSlot(object)
    def serial_com_error(self, err):
        '''
        Qt Slot function triggered when
        An Error is raised by Serial COM Module
        param: err - Exception Object
        return: None
        '''
        msg = f"{datetime.now()} : Client==> Unable to connect on Port {self.selectedPort} {err}"
        self.logEvent(self.ui.listWidget, msg)
        self.ui.pushButton_2.setEnabled(True)
        self.ui.pushButton_9.setEnabled(False)
        self.ui.pushButton_4.setEnabled(True)

    @pyqtSlot(object)
    def connect(self, ser):
        '''
        Qt slot function triggered when 
        Serial Object is ready.
        Attaches selected port & opens connection 
        using Serial Object. Then updates the Baud Rate 
        Supported by COM port.
        If connected enables Baud Rate Set Button
        Else disables it
        Button Enables if Baud Rate set:
            1. Push Command
            2. Select Image
            3. Clear Console
            4. Print Image
            5. Delete Board Image
        param: ser - serial.serialwin32.Serial object
        return: None
        '''
        try:
            self.serial = ser
            self.serial.port = self.selectedPort
            self.serial.open()
        except Exception as e:
            msg = f"{datetime.now()} : Client==> Unable to connect on Port {self.selectedPort} {e}"
            self.logEvent(self.ui.listWidget, msg)
            self.updateComButtonsOnConnect(False)
            error.exception(e)
        else:
            self.update_baud_rates(self.ui.comboBox, self.serial)
            self.update_baud_rates(self.ui.comboBox_4, self.serial)
            msg = f"{datetime.now()} : Client==> Connected to Port {self.selectedPort}"
            self.logEvent(self.ui.listWidget, msg)
            self.logEvent(self.ui.listWidget_2, msg)
            self.updateComButtonsOnConnect(True)
            # self.config_baud_rate(True)
            if self.fileTransferFlag and self.automateTask:
                self.config_baud_rate()
                time.sleep(0.2)
                self.delete_images_from_board()
                self.fileTransferFlag = False
            if self.imagesTransferFlag and self.automateTask:
                self.config_baud_rate()
                self.imagesTransferFlag = False

            self.serial.flush()

    def close(self):
        '''
        Function to Disconnect from COM port
        Disables all the operation buttons
        '''
        try:
            self.serial.close()
        except Exception as e:
            error.exception(e)
            msg = f"{datetime.now()} : Client==> Error Disconnecting {self.selectedPort} - Error : {e}"
            self.logEvent(self.ui.listWidget, msg)
            self.logEvent(self.ui.listWidget_2, msg)
        else:
            msg = f"{datetime.now()} : Client==> Port {self.selectedPort} disconnected!"
            self.logEvent(self.ui.listWidget, msg)
            self.logEvent(self.ui.listWidget_2, msg)
            self.updateComButtonsOnDisconnect(False)

            self.ui.groupBox.setHidden(True)
            self.fileTransferGif.stop()
            self.ui.progressBar_2.setValue(0)
            # self.ui.pushButton_12.setEnabled(False)

    def connect_to_com_port(self):
        '''
        Worker to Create Serial Object
        '''
        worker = ComWorker()
        worker.signals.serial_obj.connect(self.connect)
        worker.signals.error.connect(self.serial_com_error)
        self.threadpool.start(worker)

    def delete_images_from_board(self):
        '''
        Sends Command to Delete All Images from Board
        Displays a Dialogue Message to confirm Deletion 
        param: None
        return: None
        '''
        # Delete Command
        cmd = "1C 60 54 52 49 4F 58"
        self.ui.lineEdit.setText(cmd)
        self.isDeleteCMD = True
        # text = "Do you want to delete All Images?"
        # title = "Deletion!"
        # icon = QMessageBox.Question
        # buttons_options = QMessageBox.Yes | QMessageBox.No
        # dialogue = self.dialog(title,text,icon,buttons_options)

        # if dialogue == QMessageBox.Yes:
        #     self.sendCommand()
        # else:
        #     pass
        self.sendCommand()
        self.set_cmd_buttons(False)

    def config_baud_rate(self, default=False):
        '''
        Configures Baud Rate on the Connected COM port
        Enables & Disables UI buttons after setting up Baud Rate
        Buttons Enabled:
            1. Select Image
            2. Clear Console
            3. Disconnect
            4. Print Image
            5. Push Command
            6. Delete Board Images
        Buttons Disabled:
            1. Connect 
            2. Set
        param: None
        return: None
        '''
        self.serial.baudrate = self.baudrate
        msg = f"{datetime.now()} : Client==> BaudRate Set to {self.serial.baudrate}"
        self.logEvent(self.ui.listWidget, msg)
        self.logEvent(self.ui.listWidget_2, msg)

        # self.disable_com_buttons(True)
        if not default:
            self.ui.pushButton_3.setEnabled(False)  # logo baud set
            self.ui.pushButton_13.setEnabled(False)  # flash baud set

        if self.bulkTransfer:
            if isinstance(self.serial, serial.serialwin32.Serial) \
                    and self.serial.is_open \
                    and len(self.ImagesBytesArray) > 0:
                self.ui.pushButton_7.setEnabled(True)
            else:
                self.ui.pushButton_7.setEnabled(False)
        else:
            if isinstance(self.serial, serial.serialwin32.Serial) \
                    and self.file is not None \
                    and self.serial.is_open \
                    and len(self.bytesArray) > 0 \
                    and self.idx is not None \
                    and self.ref is not None:
                self.ui.pushButton_7.setEnabled(True)  # image(s) load button
            else:
                self.ui.pushButton_7.setEnabled(False)

        if isinstance(self.serial, serial.serialwin32.Serial) \
                and self.serial.is_open \
                and self.BinFile is not None:
            self.ui.pushButton_18.setEnabled(True)  # firmware update mode
        else:
            self.ui.pushButton_18.setEnabled(False)  # firmware update mode

        self.ui.comboBox.setCurrentText(f"{self.baudrate}")
        self.ui.comboBox_4.setCurrentText(f"{self.baudrate}")

    def logEvent(self, listWidget, msg):
        '''
        Log Events to List Widget
        param: listWidget - QListWidget
        param: msg - message to add to List Widget
        return: None
        '''
        item = QListWidgetItem(msg)
        listWidget.addItem(item)
        listWidget.scrollToItem(item)

    def clearConsole(self, widget):
        '''
        Function to Clear the Console
        param: widget - QListWidget
        return: None
        '''
        widget.clear()

    def select_port(self, index):
        '''
        Updates the selected port from the Ports ComboBox
        param: Index - Integer - selected Index
        return: None
        '''
        self.selectedPort = str(self.ports[index]).split("-")[0].strip()
        msg = f"{datetime.now()} : Client==> Port Selected {self.selectedPort}"
        self.logEvent(self.ui.listWidget, msg)
        self.logEvent(self.ui.listWidget_2, msg)

    def update_baud_rates(self, comboBox, serial):
        '''
        Populates Baud Rates Supported by assigned COM port
        param: combobox - QComboBox
        param: serial - serial.serialwin32.Serial Object
        '''
        comboBox.clear()
        for baud in serial.BAUDRATES:
            comboBox.addItem(f"{baud}")
        # if self.init:
        #     self.baudrate = self.flashBaudRate
        # else:
        #     self.baudrate = self.logoBaudRate
        # comboBox.setCurrentText(f"{self.baudrate}") # new line

    @pyqtSlot(tuple)
    def fileChunkTransferStatus(self, data):
        '''
        Qt function triggered after one file chunk is sent to COM port
        param: data - tuple ( str (msg), str(msg) )
        '''
        self.logEvent(self.ui.listWidget, data[0])

    @pyqtSlot(tuple)
    def error(self, data):
        '''
        Qt Slot function triggered when 
        an error is encoutner after cmd / chunk transfer
        param: data - tuple ( str(msg) , str(error object) )
        '''
        msg = f"{datetime.now()} : Client==> Error Sending " + \
            str(data[0]) + f" Error: {data[1]}"
        self.logEvent(self.ui.listWidget, msg)
        if self.isLogoCMD:
            self.isLogoCMD = False
            # if self.automateTask:
            #     time.sleep(0.1)
            #     self.bulk_img_transfer()
        elif self.isDeleteCMD:
            self.isDeleteCMD = False
            # if self.automateTask:
            #     time.sleep(0.3)
            #     self.delete_images_from_board()

    @pyqtSlot(str)
    def fileTranserComplete(self, success):
        '''
        Qt Slot function triggered after the file operation is complete
        Enables following buttons:
            1. Delete Images from Board
            2. Push Command
            3. Print Image
            4. Load Image to Board ( if image is selected )
        Hide Progress bar and sets it value to 0
        '''
        msg = f"{datetime.now()} : Client==> Done! {success}"

        self.logEvent(self.ui.listWidget, msg)
        self.isLogoCMD = False
        self.ui.progressBar.setHidden(True)
        self.ui.label_2.setHidden(True)
        self.ui.progressBar.setValue(0)
        self.pbar = 0
        self.set_cmd_buttons(True)
        if len(self.bytesArray) > 0 and self.idx is not None and self.ref is not None:
            pass
        else:
            self.ui.pushButton_7.setEnabled(False)

        if success == SUCCESS:
            if not self.ui.SelectImage.isVisible():
                self.bulkWorker.wake()
        else:
            if not self.ui.SelectImage.isVisible():
                self.bulkWorker.fail()
                self.bulkWorker.wake()

    @pyqtSlot(tuple)
    def response(self, data):
        '''
        Response received from MCU after CMD or one Image Chunk is sent
        param: data - tuple (msg (str), status msg(str) , int ( 0 or file % sent) )
        return: None
        '''

        self.logEvent(self.ui.listWidget, data[0])
        # update progress bar
        if (self.isLogoCMD and data[1] == CHUNK_SUCCESS and data[2] > 0):
            self.pbar = self.pbar + ceil(data[2]/len(self.bytesArray)*100)
            self.ui.progressBar.setValue(self.pbar)
        elif self.isLogoCMD and data[1] != CHUNK_SUCCESS:
            self.playSound(beep="beep", beepTones=1)
        elif self.isDeleteCMD and data[1] == DELETE_ACK:
            self.isDeleteCMD = False
            if self.automateTask:
                time.sleep(0.2)
                self.bulk_img_transfer()

        elif self.isDeleteCMD and data[1] == TIMEOUT:
            self.isDeleteCMD = False
            self.playSound(beep="beep", beepTones=3)
            # if self.automateTask:
            #     time.sleep(0.3)
            #     self.delete_images_from_board()
        self.isDeleteCMD = False

    def send_logo_to_board(self):
        '''
        Send Logo Image Data Chunks[array of array] to COM writer Worker
        Following signals trigger call backs:
        signal - progress --> Logo file transfer progress - in %
        signal - error --> Error during transfer - Exception
        signal - finished --> transfer task complete
        signal - response --> status during the transfer
        '''
        try:
            worker = WriteWorker(self.serial, self.data_chunks)
            self.ui.label_2.setHidden(False)
            self.ui.label_2.setText("Image Progress")
            worker.signals.progress.connect(self.fileChunkTransferStatus)
            worker.signals.response.connect(self.response)
            worker.signals.error.connect(self.error)
            worker.signals.finished.connect(self.fileTranserComplete)
            self.threadpool.start(worker)

        except Exception as e:
            msg = f"{datetime.now()} : Client==> {e}"
            self.logEvent(self.ui.listWidget, msg)
            error.exception(e)

    @pyqtSlot()
    def cmd_transfer_complete(self):
        '''
        Qt Slot function triggered after cmd task is complete
        '''
        msg = f"{datetime.now()} : Client==> Done!"
        self.logEvent(self.ui.listWidget, msg)
        self.isLogoCMD = False
        self.isDeleteCMD = False
        self.isManualCMD = False

    @pyqtSlot(tuple)
    def cmd_transfer_status(self, data):
        '''
        Qt slot function triggered after cmd is sent to COM port
        param: data - tuple ( str(msg), str(msg))
        '''
        self.logEvent(self.ui.listWidget, data[0])
        if self.serial is not None:
            if self.serial.is_open:
                self.set_cmd_buttons(True)
                self.ui.pushButton_7.setEnabled(False)
                if len(self.bytesArray) > 0 and self.idx is not None and self.ref is not None:
                    self.ui.pushButton_7.setEnabled(True)

    def sendCommand(self):
        '''
        Send commands to COM writer Worker
        Following signals trigger call backs:
        signal - progress - Progress on CMD sent
        signal - response - response after the command
        signal - error - error after the command
        signal - finished - task complete
        '''
        try:
            cmd = self.ui.lineEdit.text()
            self.set_cmd_buttons(False)
            self.ui.pushButton_8.setEnabled(True)
            msg = f"{datetime.now()} : Client==> {cmd}"
            self.logEvent(self.ui.listWidget, msg)
            worker = WriteWorker(self.serial, cmd)
            worker.signals.progress.connect(self.cmd_transfer_status)
            worker.signals.response.connect(self.response)
            worker.signals.error.connect(self.error)
            worker.signals.finished.connect(self.cmd_transfer_complete)
            self.threadpool.start(worker)

        except Exception as e:
            msg = f"{datetime.now()} : Client==> {e}"
            self.logEvent(self.ui.listWidget, msg)

    @pyqtSlot(tuple)
    def printerResponse(self, response):
        '''
        Qt Slot function triggered after receiving response from printer
        If Printer is ready( response 50 ) sends the Image Index to Print
        Enables the Buttons after print:
        1. Delete Images from board
        2. Push Command
        3. Load Image to Board ( if image is selected & port still open)
        param: response - tupe ( str( msg ) , str( msg ) )
        return: None
        '''
        msg = f"{datetime.now()} : Device==> {response[0]}{response[1]}"
        self.logEvent(self.ui.listWidget, msg)
        if response[0] == "50":
            print_img_idx = "{0:02X}".format(ord(self.printImgIdx))
            self.printCMD = "1B 4C " + str(print_img_idx)
            self.send_print_command()
        else:
            pass

        if self.serial is not None:
            if self.serial.is_open:
                self.set_cmd_buttons(True)
                self.ui.pushButton_7.setEnabled(False)
                if len(self.bytesArray) > 0 and self.idx is not None and self.ref is not None:
                    self.ui.pushButton_7.setEnabled(True)

    def send_print_command(self):
        '''
        Send Print command to Printer Worker
        Following Signals Trigger call backs:
        signal - response - response received from Printer
        signal - error - any error after sending print cmd
        '''
        try:
            worker = PrinterWorker(self.serial, self.printCMD)
            worker.signals.response.connect(self.printerResponse)
            worker.signals.error.connect(self.error)
            self.threadpool.start(worker)

        except Exception as e:
            msg = f"{datetime.now()} : Client==> {e}"
            self.logEvent(self.ui.listWidget, msg)
            error.exception(e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setDesktopSettingsAware(True)
    window = MainApp()
    window.show()
    sys.exit(app.exec())
