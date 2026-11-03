"""ATORCH TU13 USB/Type-C tester - PySide6 desktop application."""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis

from .protocol import (
    CHART_FIELDS,
    MEAS_FIELDS,
    TU13,
    TU13Error,
    device_info,
    find_device,
    in_bootloader,
)

POLL_MS = 500
HISTORY = 180


class FlashWorker(QThread):
    progress = Signal(int, int)
    log = Signal(str)
    done = Signal(bool, str)

    def __init__(self, dev_path, image_path):
        super().__init__()
        self.dev_path = dev_path
        self.image_path = image_path

    def run(self):
        device = TU13(self.dev_path)
        try:
            device.open()
            with open(self.image_path, "rb") as fh:
                image = fh.read()
            device.flash(
                image,
                progress_cb=lambda done, total: self.progress.emit(done, total),
                log_cb=self.log.emit,
            )
            self.done.emit(True, "Transfer complete - the device is flashing and rebooting")
        except Exception as exc:  # surface any failure to the UI
            self.done.emit(False, str(exc))
        finally:
            device.close()


class MeasureTab(QWidget):
    def __init__(self):
        super().__init__()
        self.t0 = None
        layout = QVBoxLayout(self)

        grid = QGridLayout()
        self.value_labels = {}
        tiles = [(label, unit) for _off, label, unit, _fmt, _div in MEAS_FIELDS]
        tiles.append(("Duration", ""))
        for i, (label, _unit) in enumerate(tiles):
            row, col = divmod(i, 3)
            name = QLabel(label)
            name.setStyleSheet("color:#888; font-size:11px;")
            value = QLabel("--")
            value.setStyleSheet("font:bold 22px 'DejaVu Sans Mono'; color:#d7e8ff;")
            grid.addWidget(name, row * 2, col)
            grid.addWidget(value, row * 2 + 1, col)
            self.value_labels[label] = value
        layout.addLayout(grid)

        self.chart = QChart()
        self.chart.setTheme(QChart.ChartThemeDark)
        self.series = []
        colours = ["#4dc3ff", "#7dff8a", "#ffd24d", "#ff8a8a"]
        for i in CHART_FIELDS:
            series = QLineSeries()
            series.setName(MEAS_FIELDS[i][1])
            pen = series.pen()
            pen.setColor(colours[i] if i < len(colours) else "#ffffff")
            pen.setWidth(2)
            series.setPen(pen)
            self.chart.addSeries(series)
            self.series.append(series)
        axis_x = QValueAxis()
        axis_x.setRange(0, HISTORY)
        axis_x.setVisible(False)
        axis_y = QValueAxis()
        axis_y.setRange(0, 30)
        self.chart.addAxis(axis_x, Qt.AlignBottom)
        self.chart.addAxis(axis_y, Qt.AlignLeft)
        for series in self.series:
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)
        self.chart.legend().setVisible(True)
        view = QChartView(self.chart)
        view.setRenderHint(view.renderHints())
        layout.addWidget(view, 1)

        self.history = {i: [] for i in CHART_FIELDS}

    def update(self, data):
        if not data:
            return
        if self.t0 is None:
            self.t0 = time.monotonic()
        for _off, label, unit, fmt, _div in MEAS_FIELDS:
            value = data.get(label)
            if value is None:
                continue
            self.value_labels[label].setText(f"{value:{fmt}} {unit}")
        elapsed = int(time.monotonic() - self.t0)
        self.value_labels["Duration"].setText(
            f"{elapsed // 3600}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
        )
        for slot, field in enumerate(CHART_FIELDS):
            label = MEAS_FIELDS[field][1]
            value = data.get(label)
            if value is None:
                continue
            history = self.history[slot]
            history.append(value)
            if len(history) > HISTORY:
                history = history[-HISTORY:]
                self.history[slot] = history
            series = self.series[slot]
            series.clear()
            for x, y in enumerate(history):
                series.append(x, y)


class DeviceTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        info = QGroupBox("Device")
        grid = QGridLayout(info)
        self.info_labels = {}
        for i, key in enumerate(["Product", "Manufacturer", "Serial", "Node", "Mode"]):
            grid.addWidget(QLabel(key + ":"), i, 0)
            value = QLabel("-")
            grid.addWidget(value, i, 1)
            self.info_labels[key] = value
        layout.addWidget(info)

        console = QGroupBox("Raw console")
        box = QVBoxLayout(console)
        row = QHBoxLayout()
        self.page_combo = QComboBox()
        self.page_combo.addItems(
            [f"0x{x:02X}" for x in (0x20, 0x21, 0x25, 0x26, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32)]
        )
        row.addWidget(self.page_combo)
        page_button = QPushButton("Send page")
        row.addWidget(page_button)
        poll_button = QPushButton("Send measurement poll")
        row.addWidget(poll_button)
        box.addLayout(row)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        box.addWidget(self.out)
        layout.addWidget(console, 1)

        self.send_cb = None
        page_button.clicked.connect(lambda: self.send_cb and self.send_cb("page"))
        poll_button.clicked.connect(lambda: self.send_cb and self.send_cb("poll"))

    def set_info(self, product, manufacturer, serial, node, mode):
        self.info_labels["Product"].setText(product or "-")
        self.info_labels["Manufacturer"].setText(manufacturer or "-")
        self.info_labels["Serial"].setText(serial or "-")
        self.info_labels["Node"].setText(node or "-")
        self.info_labels["Mode"].setText(mode or "-")

    def log(self, text):
        self.out.appendPlainText(text)


class FlashTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        self.path_edit = QLabel("no file selected")
        browse = QPushButton("Open firmware...")
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        self.size_label = QLabel("")
        layout.addWidget(self.size_label)

        self.start_btn = QPushButton("Start download")
        self.start_btn.setEnabled(False)
        layout.addWidget(self.start_btn)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

        self.image_path = None
        self.worker = None
        browse.clicked.connect(self._browse)
        self.start_btn.clicked.connect(self._start)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open firmware", "", "Firmware (*.bin)")
        if path:
            self.image_path = path
            self.path_edit.setText(path)
            self.size_label.setText(f"{os.path.getsize(path)} bytes")
            self.start_btn.setEnabled(True)

    def _start(self):
        if not self.image_path:
            return
        node = find_device()
        if node is None:
            QMessageBox.warning(self, "TU13", "Device not found")
            return
        if not in_bootloader():
            answer = QMessageBox.question(
                self,
                "TU13",
                "The tester is running its application firmware.\n"
                "Put it into bootloader mode first (page 0x03), then retry.\n\n"
                "Enter bootloader now?",
            )
            if answer == QMessageBox.Yes:
                device = TU13(node)
                try:
                    device.open()
                    device.enter_bootloader()
                finally:
                    device.close()
            return
        self.start_btn.setEnabled(False)
        self.progress.setRange(0, 100)
        self.worker = FlashWorker(node, self.image_path)
        self.worker.progress.connect(self._progress)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.done.connect(self._done)
        self.worker.start()

    def _progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _done(self, ok, message):
        self.start_btn.setEnabled(True)
        self.log.appendPlainText(("OK: " if ok else "FAIL: ") + message)
        if not ok:
            QMessageBox.critical(self, "TU13", message)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TU13 USB Tester")
        self.resize(1100, 750)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        self.measure = MeasureTab()
        self.device = DeviceTab()
        self.flash = FlashTab()
        tabs.addTab(self.measure, "Measure")
        tabs.addTab(self.device, "Device")
        tabs.addTab(self.flash, "Firmware")

        self.status = self.statusBar()
        self.dev = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(POLL_MS)

        self.device.send_cb = self._console

    def _ensure(self):
        if self.dev is not None:
            return True
        node = find_device()
        if node is None:
            return False
        try:
            self.dev = TU13(node)
            self.dev.open()
        except TU13Error as exc:
            self.status.showMessage(str(exc))
            self.dev = None
            return False
        return True

    def _refresh_info(self):
        info = device_info()
        if info:
            mode = "bootloader (IAP)" if in_bootloader() else "application"
            self.device.set_info(info[0], info[1], info[2], find_device(), mode)

    def _tick(self):
        if find_device() is None:
            if self.dev is not None:
                self.dev.close()
                self.dev = None
            self.status.showMessage("disconnected")
            return
        if not self._ensure():
            return
        self._refresh_info()
        if in_bootloader():
            self.status.showMessage("bootloader mode - no measurements")
            return
        try:
            data = self.dev.poll()
        except (OSError, TU13Error):
            data = None
        self.measure.update(data)
        if data:
            self.status.showMessage(
                f"{data['Voltage']:.3f} V  {data['Current']:.3f} A  {data['Power']:.3f} W"
            )

    def _console(self, kind):
        if not self._ensure():
            self.device.log("device not found")
            return
        try:
            if kind == "poll":
                data = self.dev.poll()
                if data:
                    self.device.log(f"poll raw: {data['raw'].hex(' ')}")
                else:
                    self.device.log("poll: no response")
            else:
                page = int(self.device.page_combo.currentText(), 16)
                reply = self.dev.page(page)
                self.device.log(
                    f"page 0x{page:02X}: " + (reply.hex(" ") if reply else "no response")
                )
        except (OSError, TU13Error) as exc:
            self.device.log(f"error: {exc}")

    def closeEvent(self, event):
        if self.dev:
            self.dev.close()
        super().closeEvent(event)


DARK_QSS = """
QWidget { background-color: #1b1d21; color: #d9dee5; }
QTabWidget::pane { border: 1px solid #2c2f34; }
QTabBar::tab { background: #23262b; color: #aeb4bd; padding: 6px 14px; }
QTabBar::tab:selected { background: #2d3138; color: #eef2f7; }
QPlainTextEdit, QComboBox, QLineEdit {
    background: #141619; color: #cfd4dc; border: 1px solid #2c2f34;
}
QPushButton {
    background: #2a2e35; color: #dfe4ea; border: 1px solid #3a3f47;
    padding: 5px 12px; border-radius: 4px;
}
QPushButton:hover { background: #343943; }
QPushButton:disabled { color: #6b7280; background: #202329; }
QGroupBox { border: 1px solid #2c2f34; margin-top: 10px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #b8bfc9; }
QProgressBar {
    background: #141619; border: 1px solid #2c2f34;
    text-align: center; color: #dfe4ea;
}
QProgressBar::chunk { background: #3d7dd8; }
QStatusBar { color: #9aa1ab; }
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
