import base64
import codecs
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

#1.0.3

# 設定存在使用者家目錄(一定可寫入);舊版存在程式旁邊的檔案仍會讀取一次
SETTINGS_FILE = Path.home() / ".ts18xx_utilities_gui.json"
LEGACY_SETTINGS_FILE = Path(__file__).with_name("ts18xx_utilities_gui_settings.json")

# 固定不變的子命令(其餘固定參數寫在 build_cmd 內)
SUBCOMMAND = "program"

# 所有可勾選的組合,燒錄時依這個順序執行
COMBOS = [(fi, cs) for fi in (0, 1, 2) for cs in (0, 1)]

DEFAULTS = {
    "exe": "ts18xx_ftdi_flash",
    "bin_dir": "",
    "speed": "12",
    "bus_width": "single",
}
FLASH_SPEEDS = ("12", "15", "20", "40")          # --speed (MHz)
FLASH_BUS_WIDTHS = ("single", "dual", "quad")    # --bus_width

# TFLX Provisioning 分頁的預設路徑
TFLX_DEFAULT_EXE = r"C:\Users\jason\tpds\2.3.9_ts18xx_v100\Scripts\ts18xx_tflx_prov.exe"
TFLX_DEFAULT_IMG = r"C:\Users\jason\.trustplatform\ts18xx_tools_assets_1.0.0rc5\build\ap_otp.bin"


# SPDM Log Paser 分頁的預設路徑(在 PATH 內時直接用檔名即可)
SPDM_DEFAULT_EXE = "ts18xx_spdm.exe"
SPDM_IFACES = ("i2c", "spi", "i3c")
SPDM_VERSIONS = ("1.1", "1.4")
# 解析時把憑證存成 .pem 的預設資料夾:跟這支 .py 檔放在同一個資料夾
SPDM_DEFAULT_PEM_DIR = os.path.dirname(os.path.abspath(__file__))
# 舊版的預設資料夾(使用者家目錄底下),設定檔裡還存著它的話會改用新的預設
SPDM_OLD_PEM_DIR = os.path.join(os.path.expanduser("~"), "ts18xx_spdm_certs")


def _spdm_initial_pem_dir(saved_value):
    if isinstance(saved_value, str) and saved_value.strip():
        def norm(p):
            return os.path.normcase(os.path.normpath(p))
        if norm(saved_value) != norm(SPDM_OLD_PEM_DIR):
            return saved_value
    return SPDM_DEFAULT_PEM_DIR


def _stream_env():
    # 讓工具(Python 程式)輸出 UTF-8,否則輸出到管道時會用 cp950,字元會亂碼
    return dict(os.environ, PYTHONUNBUFFERED="1",
                PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


class TflxProvTab(ttk.Frame):
    """ts18xx_tflx_prov.exe 的三個動作:Start Provisioning / Stash Load + Exit / Stash Clear"""

    # action -> 顯示名稱
    LABELS = {
        "write": "Start Provisioning (TFLX STASH Write)",
        "load": "Stash Load + Exit",
        "clear": "Stash Clear",
    }

    def __init__(self, parent, saved, on_change):
        super().__init__(parent)
        self.on_change = on_change
        self.proc = None
        self.action = None
        self.out_q = queue.Queue()
        self.captured = []
        self._cr_pending = False

        def pick(key, default):
            v = saved.get(key)
            return v if isinstance(v, str) and v else default

        self.vars = {
            "exe": tk.StringVar(value=pick("tflx_exe", TFLX_DEFAULT_EXE)),
            "img": tk.StringVar(value=pick("tflx_img", TFLX_DEFAULT_IMG)),
        }
        self._build_ui()
        for v in self.vars.values():
            v.trace_add("write", lambda *_: (self._update_preview(), self.on_change()))
        self._update_preview()
        self.after(100, self._poll_output)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 5}

        f = ttk.LabelFrame(self, text="路徑")
        f.pack(fill="x", padx=10, pady=(10, 4))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="ts18xx_tflx_prov.exe").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["exe"]).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_exe).grid(row=0, column=2, **pad)

        ttk.Label(f, text="ap_otp.bin").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["img"]).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_img).grid(row=1, column=2, **pad)

        self.img_status = ttk.Label(f, text="")
        self.img_status.grid(row=2, column=1, sticky="w", padx=6)

        ttk.Label(self, foreground="gray",
                  text="固定參數:--iface_type spi  -i 0(FTDI SPI, channel 0)").pack(
            anchor="w", padx=14, pady=(0, 2))
        ttk.Label(self, foreground="#c0392b",
                  text="執行任何動作前,請先按下 EVB 的 RESET,讓裝置進入 provisioning mode。").pack(
            anchor="w", padx=14, pady=(0, 2))

        # 指令預覽
        f_prev = ttk.LabelFrame(self, text="對應的指令")
        f_prev.pack(fill="x", padx=10, pady=4)
        f_prev.columnconfigure(0, weight=1)
        self.preview = tk.Text(f_prev, height=9, wrap="word", state="disabled",
                               background=self.winfo_toplevel().cget("background"), relief="flat")
        pys = ttk.Scrollbar(f_prev, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pys.set)
        self.preview.grid(row=0, column=0, sticky="ew", padx=(6, 0), pady=4)
        pys.grid(row=0, column=1, sticky="ns", pady=4)
        ttk.Button(f_prev, text="複製全部指令", command=self._copy_cmd).grid(
            row=1, column=0, columnspan=2, sticky="e", padx=6, pady=(0, 4))

        # 三個動作按鈕
        f_btn = ttk.Frame(self)
        f_btn.pack(fill="x", padx=10, pady=4)
        self.btn_write = ttk.Button(f_btn, text="⚡ Start Provisioning",
                                    command=lambda: self.run("write"))
        self.btn_write.pack(side="left")
        self.btn_load = ttk.Button(f_btn, text="↻ Stash Load + Exit",
                                   command=lambda: self.run("load"))
        self.btn_load.pack(side="left", padx=6)
        self.btn_clear = ttk.Button(f_btn, text="⌫ Stash Clear",
                                    command=lambda: self.run("clear"))
        self.btn_clear.pack(side="left")
        self.action_btns = [self.btn_write, self.btn_load, self.btn_clear]

        f_btn2 = ttk.Frame(self)
        f_btn2.pack(fill="x", padx=10, pady=(0, 4))
        self.btn_stop = ttk.Button(f_btn2, text="■ 停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(f_btn2, text="清除輸出", command=self._clear_log).pack(side="left", padx=6)
        self.status = tk.StringVar(value="就緒")
        ttk.Label(f_btn2, textvariable=self.status).pack(side="right")

        # 輸出
        f_log = ttk.LabelFrame(self, text="輸出")
        f_log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        f_log.rowconfigure(0, weight=1)
        f_log.columnconfigure(0, weight=1)
        self.log = tk.Text(f_log, wrap="none", state="disabled", font=("Consolas", 10))
        ys = ttk.Scrollbar(f_log, orient="vertical", command=self.log.yview)
        xs = ttk.Scrollbar(f_log, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.log.tag_configure("err", foreground="#c0392b")
        self.log.tag_configure("ok", foreground="#1e8449")
        self.log.tag_configure("info", foreground="#2471a3")

    # ---------- 指令組合 ----------
    def build_cmd(self, action):
        exe = self.vars["exe"].get().strip()
        base = [exe, "--iface_type", "spi", "-i", "0"]
        if action == "write":
            return base + ["write", "--img_in", self.vars["img"].get().strip(), "--stash"]
        if action == "load":
            return base + ["exit", "stash"]
        return base + ["cmd", "--stash_clear"]

    def cmdline(self, action):
        line = subprocess.list2cmdline(self.build_cmd(action))
        # 工具會詢問是否更新韌體 [y/N],GUI 會自動回答 n,命令列要用管線送入
        return f'echo n | {line}' if action == "write" else line

    def _update_preview(self):
        img = self.vars["img"].get().strip()
        if not img:
            self.img_status.configure(text="", foreground="gray")
        elif Path(img).is_file():
            self.img_status.configure(text="✔ 找到 ap_otp.bin", foreground="#1e8449")
        else:
            self.img_status.configure(text="✘ 找不到 ap_otp.bin", foreground="#c0392b")

        text = "\n\n".join(f"[{self.LABELS[a]}]\n{self.cmdline(a)}"
                           for a in ("write", "load", "clear"))
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(self.cmdline(a) for a in ("write", "load", "clear")))
        self.status.set("已複製指令")

    # ---------- 檔案選擇 ----------
    def _browse_exe(self):
        init = os.path.dirname(self.vars["exe"].get()) or None
        if init and not os.path.isdir(init):
            init = None
        p = filedialog.askopenfilename(title="選擇 ts18xx_tflx_prov.exe", initialdir=init,
                                       filetypes=[("可執行檔", "*.exe *.bat *.cmd"), ("所有檔案", "*.*")])
        if p:
            self.vars["exe"].set(os.path.normpath(p))

    def _browse_img(self):
        init = os.path.dirname(self.vars["img"].get()) or None
        if init and not os.path.isdir(init):
            init = None
        p = filedialog.askopenfilename(title="選擇 ap_otp.bin", initialdir=init,
                                       filetypes=[("OTP binary", "*.bin"), ("所有檔案", "*.*")])
        if p:
            self.vars["img"].set(os.path.normpath(p))

    # ---------- 執行 ----------
    def _set_running(self, running):
        for b in self.action_btns:
            b.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def run(self, action):
        if self.proc is not None:
            return
        if not self.vars["exe"].get().strip():
            messagebox.showwarning("缺少參數", "請先填入 ts18xx_tflx_prov.exe 的路徑。")
            return
        if action == "write":
            img = self.vars["img"].get().strip()
            if not img or not Path(img).is_file():
                messagebox.showerror("找不到檔案", f"找不到 ap_otp.bin:\n{img}")
                return

        cmd = self.build_cmd(action)
        self.action = action
        self.captured = []
        self._append(f"\n===== {self.LABELS[action]} =====\n", "info")
        self._append(f"> {self.cmdline(action)}\n", "info")
        self.status.set(f"執行中:{self.LABELS[action]}")
        self._set_running(True)

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE if action == "write" else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, env=_stream_env(), creationflags=flags)
        except FileNotFoundError:
            self._append("找不到 ts18xx_tflx_prov.exe,請確認路徑。\n", "err")
            self._set_running(False)
            self.status.set("失敗:找不到執行檔")
            return
        except Exception as e:
            self._append(f"啟動失敗:{e}\n", "err")
            self._set_running(False)
            self.status.set("失敗")
            return

        if action == "write":
            # 等同 "n" | ...:自動回答「是否更新韌體」的 [y/N] 提示
            try:
                self.proc.stdin.write(b"n\n")
                self.proc.stdin.close()
            except OSError:
                pass

        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            text = dec.decode(chunk)
            if text:
                self.out_q.put(("data", text))
        proc.wait()
        self.out_q.put(("done", proc.returncode))

    def _poll_output(self):
        try:
            while True:
                kind, data = self.out_q.get_nowait()
                if kind == "data":
                    self.captured.append(data)
                    self._append_stream(data)
                else:
                    self._finish(data)
        except queue.Empty:
            pass
        self.after(100, self._poll_output)

    def _judge(self, action, code, text):
        """這個工具失敗時結束碼仍是 0,所以要看輸出文字判斷。"""
        if code != 0:
            return False
        if re.search(r"\[ERROR|Traceback", text):
            return False
        if action == "write":
            return "bytes match" in text and "left provisioning mode" in text
        if action == "clear":
            return "erased from on-device flash" in text
        # exit stash:要同時看到「還原 OTP 值」與「離開 provisioning mode」
        return "Restoring OTP values" in text and "left provisioning mode" in text

    def _finish(self, code):
        self.proc = None
        self._end_stream_line()
        text = "".join(self.captured)
        ok = self._judge(self.action, code, text)
        name = self.LABELS[self.action]
        if ok:
            self._append(f"[完成] {name}\n", "ok")
            self.status.set(f"完成:{name}")
        else:
            self._append(f"[失敗] {name}(結束碼 {code})\n", "err")
            if "RW_ERROR" in text:
                self._append("提示:請先按下 EVB 的 RESET 讓裝置進入 provisioning mode,再重試。\n", "err")
            self.status.set(f"失敗:{name}")
        self._set_running(False)

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            self._append("[已要求停止]\n", "err")

    def terminate(self):
        if self.proc is not None:
            self.proc.terminate()

    # ---------- 輸出 ----------
    def _append(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _append_stream(self, text):
        """\\r 代表回到行首,下一段文字覆蓋同一行。"""
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
        self.log.configure(state="normal")
        for tok in re.split(r"(\r\n|\r|\n)", text):
            if not tok:
                continue
            if tok == "\r":
                self._cr_pending = True
            elif tok in ("\n", "\r\n"):
                self.log.insert("end", "\n")
                self._cr_pending = False
            else:
                if self._cr_pending:
                    self.log.delete("end-1c linestart", "end-1c")
                    self._cr_pending = False
                self.log.insert("end", tok)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _end_stream_line(self):
        self._cr_pending = False
        self.log.configure(state="normal")
        if self.log.get("end-2c", "end-1c") not in ("\n", ""):
            self.log.insert("end", "\n")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


# ts18xx_spdm 的輸出行:[INFO 2026-11-12 10:49:53,262 tpds.ts18xx_utilities.spdm] 訊息
_SPDM_LINE_RE = re.compile(r"^\[(\w+) [0-9-]+ [0-9:,]+ [^\]]*\] ?(.*)$")
_SPDM_MEAS_RE = re.compile(
    r"MeasurementBlockIndex: 0x([0-9A-Fa-f]+), MeasurementType: (.+?), "
    r"MeasurementValue 0x([0-9A-Fa-f]+) bytes: ([0-9A-Fa-f]*)")


def _spdm_mark(msg):
    """依訊息文字判斷是 ✔ / ✘ / 一般資訊,回傳 (符號, tag)。"""
    low = msg.lower()
    if re.search(r"fail|error|mismatch|invalid|not verified", low):
        return "✘", "err"
    if "successful" in low or "verified" in low:
        return "✔", "ok"
    return " ", None


def _spdm_ascii(hexv):
    """hex 內容全是可見 ASCII 字元(例如版本字串)時,回傳對應文字,否則回傳空字串。"""
    try:
        raw = bytes.fromhex(hexv)
    except ValueError:
        return ""
    if len(raw) >= 3 and all(32 <= b < 127 for b in raw):
        return raw.decode("ascii")
    return ""


_DER_OIDS = {"2.5.4.3": "CN", "2.5.4.10": "O", "2.5.4.11": "OU",
             "2.5.4.6": "C", "2.5.4.5": "serialNumber"}
# 雜湊長度(bytes) -> (hashlib 名稱, 顯示名稱)
_HASH_ALGOS = {32: ("sha256", "SHA-256"), 48: ("sha384", "SHA-384"), 64: ("sha512", "SHA-512")}


def _der_read(buf, pos):
    """讀一個 DER TLV,回傳 (tag, 內容起點, 內容終點)。"""
    tag = buf[pos]
    n = buf[pos + 1]
    pos += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[pos:pos + k], "big")
        pos += k
    return tag, pos, pos + n


def _der_oid(content):
    parts = [content[0] // 40, content[0] % 40]
    val = 0
    for b in content[1:]:
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(val)
            val = 0
    return ".".join(str(p) for p in parts)


def _der_name(buf, start, end):
    """X.509 Name 轉成 "O=..., CN=..." 文字(每個欄位只取第一組屬性)。"""
    parts = []
    pos = start
    while pos < end:
        _, rdn_s, rdn_e = _der_read(buf, pos)      # RDN (SET)
        _, atv_s, _ = _der_read(buf, rdn_s)        # AttributeTypeAndValue (SEQUENCE)
        _, oid_s, oid_e = _der_read(buf, atv_s)    # OID
        _, val_s, val_e = _der_read(buf, oid_e)    # 值
        oid = _der_oid(buf[oid_s:oid_e])
        parts.append(f"{_DER_OIDS.get(oid, oid)}={buf[val_s:val_e].decode('utf-8', 'replace')}")
        pos = rdn_e
    return ", ".join(parts)


def _der_time(tag, content):
    s = content.decode("ascii", "replace")
    if tag == 0x17:  # UTCTime
        yy = int(s[:2])
        year, rest = (1900 + yy if yy >= 50 else 2000 + yy), s[2:]
    else:            # GeneralizedTime
        year, rest = int(s[:4]), s[4:]
    return f"{year}-{rest[0:2]}-{rest[2:4]} {rest[4:6]}:{rest[6:8]}:{rest[8:10]}Z"


def _x509_summary(der):
    """從 DER 憑證取出 Subject / Issuer / 有效期間;解析失敗就回傳空字典。"""
    try:
        _, pos, _ = _der_read(der, 0)               # Certificate
        _, pos, _ = _der_read(der, pos)             # tbsCertificate
        tag, _, end = _der_read(der, pos)
        if tag == 0xA0:                             # [0] version(v1 憑證沒有這一段)
            pos = end
            _, _, end = _der_read(der, pos)
        pos = end                                   # serialNumber 之後
        _, _, pos = _der_read(der, pos)             # signature AlgorithmIdentifier
        _, s, e = _der_read(der, pos)               # issuer
        issuer = _der_name(der, s, e)
        _, s, pos = _der_read(der, e)               # validity
        t1, a, b = _der_read(der, s)
        not_before = _der_time(t1, der[a:b])
        t2, a, b = _der_read(der, b)
        not_after = _der_time(t2, der[a:b])
        _, s, e = _der_read(der, pos)               # subject
        subject = _der_name(der, s, e)
        return {"subject": subject, "issuer": issuer,
                "not_before": not_before, "not_after": not_after}
    except Exception:
        return {}


def _spdm_split_chain(rsp_hex, digest_hex):
    """切開 Get Certificate 回應裡的憑證鏈。

    回傳 (chain, hsize, certs, complete):chain 是憑證鏈結構本身,hsize 是 RootHash 的長度
    (找不到時為 None),certs 是 [(offset, DER bytes), ...],complete 表示資料完整、沒有被截斷。
    """
    rsp = bytes.fromhex(rsp_hex)
    portion = int.from_bytes(rsp[4:6], "little")      # 回應檔頭 8 bytes:版本、代碼、Param1/2、兩個長度
    chain = rsp[8:8 + portion]
    hsize = None
    for cand in ([len(digest_hex) // 2] if digest_hex else []) + [48, 32, 64]:
        if cand in _HASH_ALGOS and len(chain) > 4 + cand and chain[4 + cand] == 0x30:
            hsize = cand
            break
    if hsize is None:
        return chain, None, [], True
    certs = []
    complete = len(chain) >= int.from_bytes(chain[0:2], "little")
    pos = 4 + hsize
    try:
        while pos + 4 <= len(chain) and chain[pos] == 0x30:
            _, _, end = _der_read(chain, pos)
            if end > len(chain):
                complete = False
                break
            certs.append((pos, chain[pos:end]))
            pos = end
    except IndexError:
        complete = False
    return chain, hsize, certs, complete


def extract_spdm_certs(text):
    """從 ts18xx_spdm 的輸出取出憑證鏈裡每張憑證的 DER,回傳 [bytes, ...];找不到就回傳空 list。"""
    m = re.search(r"Name: Get All Certs, WriteBytes: [0-9A-Fa-f]*, ReadBytes: ([0-9A-Fa-f]+)", text)
    if not m:
        return []
    d = re.search(r"Cert Chain Digest: ([0-9A-Fa-f]+)", text)
    _, _, certs, _ = _spdm_split_chain(m.group(1), d.group(1) if d else "")
    return [der for _, der in certs]


def der_to_pem(der):
    """DER 憑證轉成 PEM 文字(Base64,每行 64 個字元)。"""
    b64 = base64.b64encode(der).decode("ascii")
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"


def _spdm_chain_rows(rsp_hex, digest_hex):
    """解析 Get Certificate 的回應,回傳「憑證鏈結構」的說明與實際資料(顯示列)。"""
    chain, hsize, certs, complete = _spdm_split_chain(rsp_hex, digest_hex)
    if hsize is None:
        return []
    algo, algo_name = _HASH_ALGOS[hsize]
    length = int.from_bytes(chain[0:2], "little")
    root_hash = chain[4:4 + hsize]

    rows = [
        ("結構說明(SPDM 規格 DSP0274 的 Certificate chain format):", None),
        ("Offset  Size  Field         說明", None),
        ("0       2     Length        整個結構的總長度(含 Length、Reserved、RootHash 與所有憑證)", None),
        ("2       2     Reserved      保留,值為 0", None),
        ("4       H     RootHash      根憑證(DER)的雜湊;H 為雜湊長度(SHA-384 為 48)", None),
        ("4+H     其餘  Certificates  DER 編碼的 X.509 v3 憑證,由根憑證依序排到最末端憑證", None),
        ("Cert Chain Digest = Hash(整個結構,從 Length 欄位開始)", None),
        ("", None),
        ("實際資料:", None),
        ("Offset  Size  Field         值", None),
        (f"{0:<8}{2:<6}{'Length':<14}{length} (0x{length:04X})", None),
        (f"{2:<8}{2:<6}{'Reserved':<14}0x{chain[2:4].hex().upper()}", None),
        (f"{4:<8}{hsize:<6}{'RootHash':<14}{root_hash.hex().upper()}", None),
    ]
    certs_rows = []
    for idx, (pos, der) in enumerate(certs):
        info = _x509_summary(der)
        h = hashlib.new(algo, der).digest()
        certs_rows.append((f"cert{idx}  offset {pos}  {len(der)} bytes"
                           + ("  (根憑證)" if idx == 0 else ""), None))
        if info:
            certs_rows += [(f"    Subject:  {info['subject']}", None),
                           (f"    Issuer:   {info['issuer']}", None),
                           (f"    Validity: {info['not_before']} ~ {info['not_after']}", None)]
        certs_rows.append((f"    {algo_name}(cert{idx}) = {h.hex().upper()}", None))
        if idx == 0:  # 根憑證的雜湊要等於憑證鏈檔頭裡的 RootHash
            ok = h == root_hash
            certs_rows.append((f"    {'✔' if ok else '✘'} 與 RootHash {'相符' if ok else '不相符'}",
                               "ok" if ok else "err"))
    if not complete:
        certs_rows.append(("(憑證資料不完整,後面的憑證無法解析)", "err"))
    rows.append((f"{4 + hsize:<8}{max(len(chain) - 4 - hsize, 0):<6}{'Certificates':<14}"
                 f"共 {len(certs)} 張", None))
    rows += certs_rows

    if digest_hex:
        rows.append(("", None))
        if len(chain) == length:
            h = hashlib.new(algo, chain).digest().hex().upper()
            ok = h == digest_hex.upper()
            rows.append((f"Cert Chain Digest = {algo_name}(上述整個結構,共 {length} bytes)", None))
            rows.append((f"    = {h}", None))
            rows.append((f"    {'✔' if ok else '✘'} "
                         f"與裝置回報的 Cert Chain Digest {'相符' if ok else '不相符'}",
                         "ok" if ok else "err"))
        else:
            rows.append(("(回應只包含部分憑證鏈,無法重新計算 Cert Chain Digest)", None))
    return rows


def parse_spdm_output(text, code):
    """把 ts18xx_spdm 的輸出整理成 [(文字, tag), ...];tag 為 None / hdr / ok / err。"""
    entries = []  # [等級, 訊息];沒有時間戳記的行(例如 hex dump)接在上一筆後面
    for raw in text.splitlines():
        m = _SPDM_LINE_RE.match(raw)
        if m:
            entries.append([m.group(1), m.group(2)])
        elif entries and raw.strip():
            entries[-1][1] += "\n" + raw
    if not entries:
        return [("輸出中沒有可解析的 SPDM 紀錄。\n", "err")]
    msgs = [e[1] for e in entries]

    def find(pattern):
        for msg in msgs:
            m = re.search(pattern, msg)
            if m:
                return m
        return None

    def verdict(pattern):
        m = find(pattern)
        return None if m is None else _spdm_mark(m.group(0))[0] == "✔"

    # --- 連線資訊 ---
    rows_conn = []
    m = find(r"SPDM Interface Type: (\w+), Certificate Chain Slot index: (\d+), "
             r"Host Device Address: (\d+)")
    if m:
        rows_conn += [(f"Interface: {m.group(1).upper()}", None),
                      (f"Cert Chain Slot: {m.group(2)}", None),
                      (f"Host Device Address: {m.group(3)}", None)]
    m = find(r"SPI Mode: (\S+)")
    if m:
        rows_conn.append((f"SPI Mode: {m.group(1)}", None))
    m = find(r"Responder DataTransferSize: (\d+), MaxSPDMmsgSize: (\d+), "
             r"CHUNK_CAP flag: (\w+), Chunking enabled: (\w+)")
    if m:
        rows_conn += [(f"Responder DataTransferSize: {m.group(1)} bytes", None),
                      (f"Responder MaxSPDMmsgSize: {m.group(2)} bytes", None),
                      (f"Chunking: CAP {m.group(3)}, enabled {m.group(4)}", None)]

    # --- 憑證 ---
    rows_cert = []
    m = find(r"CERT_SIZE: slot (\d+) total_length=(\d+)")
    if m:
        rows_cert.append((f"Slot {m.group(1)} 憑證鏈總長度: {int(m.group(2))} bytes", None))
    cert_ok = []
    for msg in msgs:
        m = re.match(r"cert(\d+)[: ]\s*(.*)", msg)
        if m:
            sym, tag = _spdm_mark(msg)
            rows_cert.append((f"{sym} cert{m.group(1)}: {m.group(2)}", tag))
            if "verification" in msg.lower():
                cert_ok.append(sym == "✔")
    m = find(r"Root Certificate Hash: ([0-9A-Fa-f]+)")
    if m:
        rows_cert.append((f"  Root Certificate Hash: {m.group(1)}", None))
    for pattern in (r"Root Certificate Hash Digest.*", r"Cert Chain Digest: ([0-9A-Fa-f]+)",
                    r"Cert Chain Digest \w.*"):
        m = find(pattern)
        if m:
            sym, tag = _spdm_mark(m.group(0))
            rows_cert.append((f"{sym} {m.group(0)}", tag))

    # --- 憑證鏈結構(從 Get Certificate 的回應位元組解析) ---
    rows_chain = []
    m_rsp = find(r"Name: Get All Certs, WriteBytes: [0-9A-Fa-f]*, ReadBytes: ([0-9A-Fa-f]+)")
    if m_rsp:
        m_dig = find(r"Cert Chain Digest: ([0-9A-Fa-f]+)")
        try:
            rows_chain = _spdm_chain_rows(m_rsp.group(1), m_dig.group(1) if m_dig else "")
        except Exception as e:
            rows_chain = [(f"憑證鏈結構解析失敗:{e}", "err")]

    # --- 簽章 ---
    rows_sig = []
    for pattern in (r"Challenge Signature Verification.*",
                    r"Measurements Signature Verification.*"):
        m = find(pattern)
        if m:
            sym, tag = _spdm_mark(m.group(0))
            rows_sig.append((f"{sym} {m.group(0)}", tag))

    # --- 量測區塊(同一個 index 以第一次出現的為準,也就是單獨讀取的那一筆) ---
    blocks = {}
    for msg in msgs:
        for m in _SPDM_MEAS_RE.finditer(msg):
            blocks.setdefault(int(m.group(1), 16), (m.group(2), int(m.group(3), 16), m.group(4)))
    rows_meas = []
    m = find(r"Measurement blocks reported: \[([0-9, ]*)\]")
    if m:
        rows_meas.append((f"回報的區塊: {m.group(1)}", None))
    for idx in sorted(blocks):
        typ, size, hexv = blocks[idx]
        if hexv and set(hexv) <= {"0"}:
            val = "(全為 0)"
        elif len(hexv) > 96:
            val = hexv[:96] + "…"
        else:
            val = hexv
        ascii_text = _spdm_ascii(hexv)
        if ascii_text:
            val += f'  (ASCII: "{ascii_text}")'
        rows_meas.append((f"Block {idx:>2}  {typ}  ({size} bytes)", None))
        rows_meas.append((f"    {val}", None))

    # --- 其他資訊(BMC / GPIO 量測) ---
    rows_extra = [(msg, None) for msg in msgs
                  if re.match(r"(INDEX\d+|BMC measurement|\s*secure_gpio|\s*attest_gpio_in)", msg)]

    # --- 驗證項目與整體結果 ---
    checks = [
        ("Root Certificate Hash Digest", verdict(r"Root Certificate Hash Digest.*")),
        ("Cert Chain Digest", verdict(r"Cert Chain Digest \w.*")),
        ("憑證鏈 ECDSA 驗證", (all(cert_ok) if cert_ok else None)),
        ("Challenge 簽章驗證", verdict(r"Challenge Signature Verification.*")),
        ("Measurements 簽章驗證", verdict(r"Measurements Signature Verification.*")),
    ]
    problems = [msg for level, msg in entries if level in ("WARNING", "ERROR", "CRITICAL")]
    if "Traceback" in text:
        problems.append("輸出包含 Python Traceback")
    failed = code != 0 or bool(problems) or any(state is False for _, state in checks)

    if failed:
        head = ("整體結果: FAIL", "err")
    elif all(state for _, state in checks):
        head = ("整體結果: PASS", "ok")
    else:
        head = ("整體結果: 執行完成,但部分驗證項目沒有出現在輸出中", "hdr")
    out = [(head[0] + "\n\n", head[1])]

    def section(title, rows):
        if not rows:
            return
        out.append((f"■ {title}\n", "hdr"))
        for line, tag in rows:
            for sub in line.split("\n"):
                out.append((f"  {sub}\n", tag))
        out.append(("\n", None))

    section("驗證項目", [
        (f"{'✔' if st else ('✘' if st is False else '—')} {label}",
         "ok" if st else ("err" if st is False else None))
        for label, st in checks])
    section("警告 / 錯誤", [(msg, "err") for msg in problems])
    section("連線資訊", rows_conn)
    section("憑證", rows_cert)
    section("憑證鏈結構", rows_chain)
    section("簽章", rows_sig)
    section("量測區塊", rows_meas)
    section("其他資訊", rows_extra)
    return out


class SpdmLogTab(ttk.Frame):
    """SPDM Log Paser 分頁:執行 ts18xx_spdm,上方顯示原始輸出,下方顯示解析後的結果"""

    def __init__(self, parent, saved, on_change):
        super().__init__(parent)
        self.on_change = on_change
        self.proc = None
        self.out_q = queue.Queue()
        self.captured = []
        self._cr_pending = False

        def pick(key, default, allowed=None):
            v = saved.get(key)
            if isinstance(v, str) and v and (allowed is None or v in allowed):
                return v
            return default

        self.vars = {
            "exe": tk.StringVar(value=pick("spdm_exe", SPDM_DEFAULT_EXE)),
            "iface": tk.StringVar(value=pick("spdm_iface", "spi", SPDM_IFACES)),
            "port": tk.StringVar(value=pick("spdm_port", "0")),
            "slot": tk.StringVar(value=pick("spdm_slot", "0")),
            "version": tk.StringVar(value=pick("spdm_version", "1.4", SPDM_VERSIONS)),
            "pem_dir": tk.StringVar(value=_spdm_initial_pem_dir(saved.get("spdm_pem_dir"))),
            "save_pem": tk.BooleanVar(
                value=saved["spdm_save_pem"] if isinstance(saved.get("spdm_save_pem"), bool) else True),
        }
        self._build_ui()
        for var in self.vars.values():
            var.trace_add("write", lambda *_: (self._update_preview(), self.on_change()))
        self._update_preview()
        # 選到 I2C 時,在輸出視窗先提示要用 Aardvark I2C Port
        self.vars["iface"].trace_add("write", lambda *_: self._i2c_hint())
        self._i2c_hint()
        self.after(100, self._poll_output)

    def _build_ui(self):
        pad = {"padx": 6, "pady": 5}

        f = ttk.LabelFrame(self, text="路徑")
        f.pack(fill="x", padx=10, pady=(10, 4))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="ts18xx_spdm.exe").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["exe"]).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_exe).grid(row=0, column=2, **pad)

        ttk.Label(f, text="憑證輸出資料夾").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["pem_dir"]).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_pem_dir).grid(row=1, column=2, **pad)
        ttk.Checkbutton(f, text="解析時將憑證存成 cert0.pem、cert1.pem …",
                        variable=self.vars["save_pem"]).grid(row=2, column=1, sticky="w", **pad)

        # 選項
        f_opt = ttk.LabelFrame(self, text="選項")
        f_opt.pack(fill="x", padx=10, pady=4)

        ttk.Label(f_opt, text="Interface").grid(row=0, column=0, sticky="e", **pad)
        f_if = ttk.Frame(f_opt)
        f_if.grid(row=0, column=1, sticky="w", **pad)
        for val, label in (("i2c", "I2C"), ("spi", "SPI"), ("i3c", "I3C")):
            ttk.Radiobutton(f_if, text=label, value=val,
                            variable=self.vars["iface"]).pack(side="left", padx=(0, 10))

        ttk.Label(f_opt, text="SPT Channel").grid(row=0, column=2, sticky="e", padx=(24, 6), pady=5)
        ttk.Entry(f_opt, textvariable=self.vars["port"], width=6).grid(
            row=0, column=3, sticky="w", **pad)

        ttk.Label(f_opt, text="Cert Chain Slot (0–7)").grid(row=1, column=0, sticky="e", **pad)
        ttk.Spinbox(f_opt, from_=0, to=7, width=6, textvariable=self.vars["slot"]).grid(
            row=1, column=1, sticky="w", **pad)

        ttk.Label(f_opt, text="SPDM Version").grid(row=1, column=2, sticky="e", padx=(24, 6), pady=5)
        f_ver = ttk.Frame(f_opt)
        f_ver.grid(row=1, column=3, sticky="w", **pad)
        for val in SPDM_VERSIONS:
            ttk.Radiobutton(f_ver, text=val, value=val,
                            variable=self.vars["version"]).pack(side="left", padx=(0, 10))

        # 指令預覽
        f_prev = ttk.LabelFrame(self, text="對應的指令")
        f_prev.pack(fill="x", padx=10, pady=4)
        self.preview = tk.Text(f_prev, height=2, wrap="word", state="disabled",
                               background=self.winfo_toplevel().cget("background"), relief="flat")
        self.preview.pack(fill="x", padx=6, pady=4)

        # 按鈕
        f_btn = ttk.Frame(self)
        f_btn.pack(fill="x", padx=10, pady=4)
        self.btn_run = ttk.Button(f_btn, text="▶ Run", command=self.run)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(f_btn, text="■ 停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(f_btn, text="清除輸出", command=self._clear_log).pack(side="left")
        self.status = tk.StringVar(value="就緒")
        ttk.Label(f_btn, textvariable=self.status).pack(side="right")

        # 輸出與解析結果:上下兩個視窗,可拖曳中間的分隔線調整大小
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        def make_text(title):
            fr = ttk.LabelFrame(pw, text=title)
            fr.rowconfigure(0, weight=1)
            fr.columnconfigure(0, weight=1)
            txt = tk.Text(fr, wrap="none", state="disabled", font=("Consolas", 10))
            ys = ttk.Scrollbar(fr, orient="vertical", command=txt.yview)
            xs = ttk.Scrollbar(fr, orient="horizontal", command=txt.xview)
            txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
            txt.grid(row=0, column=0, sticky="nsew")
            ys.grid(row=0, column=1, sticky="ns")
            xs.grid(row=1, column=0, sticky="ew")
            txt.tag_configure("err", foreground="#c0392b")
            txt.tag_configure("ok", foreground="#1e8449")
            txt.tag_configure("info", foreground="#2471a3")
            txt.tag_configure("hdr", font=("Consolas", 10, "bold"))
            pw.add(fr, weight=1)
            return txt

        self.log = make_text("輸出")
        self.parsed = make_text("解析結果")

    def _browse_exe(self):
        init = os.path.dirname(self.vars["exe"].get()) or None
        if init and not os.path.isdir(init):
            init = None
        p = filedialog.askopenfilename(title="選擇 ts18xx_spdm.exe", initialdir=init,
                                       filetypes=[("可執行檔", "*.exe *.bat *.cmd"), ("所有檔案", "*.*")])
        if p:
            self.vars["exe"].set(os.path.normpath(p))

    def _browse_pem_dir(self):
        init = self.vars["pem_dir"].get().strip() or None
        if init and not os.path.isdir(init):
            init = None
        p = filedialog.askdirectory(title="選擇憑證(.pem)輸出資料夾", initialdir=init)
        if p:
            self.vars["pem_dir"].set(os.path.normpath(p))

    # ---------- 指令組合 ----------
    def build_cmd(self):
        return [self.vars["exe"].get().strip(),
                "--iface_type", self.vars["iface"].get(),
                "--port", self.vars["port"].get().strip(),
                "--slot_index", self.vars["slot"].get().strip(),
                "--spdm_version", self.vars["version"].get()]

    def _update_preview(self):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", subprocess.list2cmdline(self.build_cmd()))
        self.preview.configure(state="disabled")

    def _i2c_hint(self):
        """選 I2C 時顯示提示;換成 SPI / I3C 時把輸出視窗裡的提示全部清掉。"""
        if self.vars["iface"].get() == "i2c":
            ranges = self.log.tag_ranges("i2c_hint")
            # 輸出視窗最後一段已經是提示(例如重複點選 I2C),就不再重複加
            if ranges and self.log.compare(ranges[-1], ">=", "end-1c"):
                return
            self._append("[提示] 使用 I2C 介面時,需使用 Aardvark I2C Port\n", ("info", "i2c_hint"))
        else:
            ranges = self.log.tag_ranges("i2c_hint")
            if not ranges:
                return
            self.log.configure(state="normal")
            for i in range(len(ranges) - 2, -1, -2):
                self.log.delete(ranges[i], ranges[i + 1])
            self.log.configure(state="disabled")

    # ---------- 執行 ----------
    def _set_running(self, running):
        self.btn_run.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def run(self):
        if self.proc is not None:
            return
        if not self.vars["exe"].get().strip():
            messagebox.showwarning("缺少參數", "請先填入 ts18xx_spdm.exe 的路徑。")
            return
        if not re.fullmatch(r"[0-9]+", self.vars["port"].get().strip()):
            messagebox.showerror("參數錯誤", "SPT Channel 必須是非負整數。")
            return
        slot = self.vars["slot"].get().strip()
        if not re.fullmatch(r"[0-9]+", slot) or int(slot) > 7:
            messagebox.showerror("參數錯誤", "Cert Chain Slot 必須是 0–7 的整數。")
            return

        cmd = self.build_cmd()
        self.captured = []
        self._clear_parsed()
        self._append("\n===== SPDM Run =====\n", "info")
        self._i2c_hint()
        self._append(f"> {subprocess.list2cmdline(cmd)}\n", "info")
        self.status.set("執行中")
        self._set_running(True)

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, env=_stream_env(), creationflags=flags)
        except FileNotFoundError:
            self._append("找不到 ts18xx_spdm.exe,請確認路徑或 PATH 設定。\n", "err")
            self._set_running(False)
            self.status.set("失敗:找不到執行檔")
            return
        except Exception as e:
            self._append(f"啟動失敗:{e}\n", "err")
            self._set_running(False)
            self.status.set("失敗")
            return

        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            text = dec.decode(chunk)
            if text:
                self.out_q.put(("data", text))
        proc.wait()
        self.out_q.put(("done", proc.returncode))

    def _poll_output(self):
        try:
            while True:
                kind, data = self.out_q.get_nowait()
                if kind == "data":
                    self.captured.append(data)
                    self._append_stream(data)
                else:
                    self._finish(data)
        except queue.Empty:
            pass
        self.after(100, self._poll_output)

    def _finish(self, code):
        self.proc = None
        self._end_stream_line()
        self._show_parsed("".join(self.captured), code)
        if code == 0:
            self._append("[完成] 結束碼 0\n", "ok")
            self.status.set("完成")
        else:
            self._append(f"[失敗] 結束碼 {code}\n", "err")
            self.status.set(f"失敗(結束碼 {code})")
        self._set_running(False)

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            self._append("[已要求停止]\n", "err")

    def terminate(self):
        if self.proc is not None:
            self.proc.terminate()

    # ---------- 輸出 ----------
    def _append(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _append_stream(self, text):
        """\\r 代表回到行首,下一段文字覆蓋同一行。"""
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
        self.log.configure(state="normal")
        for tok in re.split(r"(\r\n|\r|\n)", text):
            if not tok:
                continue
            if tok == "\r":
                self._cr_pending = True
            elif tok in ("\n", "\r\n"):
                self.log.insert("end", "\n")
                self._cr_pending = False
            else:
                if self._cr_pending:
                    self.log.delete("end-1c linestart", "end-1c")
                    self._cr_pending = False
                self.log.insert("end", tok)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _end_stream_line(self):
        self._cr_pending = False
        self.log.configure(state="normal")
        if self.log.get("end-2c", "end-1c") not in ("\n", ""):
            self.log.insert("end", "\n")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._clear_parsed()

    def _clear_parsed(self):
        self.parsed.configure(state="normal")
        self.parsed.delete("1.0", "end")
        self.parsed.configure(state="disabled")

    def _show_parsed(self, text, code):
        """工具執行結束後,把輸出整理到「解析結果」視窗。"""
        try:
            rows = parse_spdm_output(text, code)
        except Exception as e:
            rows = [(f"解析失敗:{e}\n", "err")]
        if self.vars["save_pem"].get():
            rows = rows + self._save_pem_files(text)
        self.parsed.configure(state="normal")
        self.parsed.delete("1.0", "end")
        for line, tag in rows:
            if tag:
                self.parsed.insert("end", line, tag)
            else:
                self.parsed.insert("end", line)
        self.parsed.see("1.0")
        self.parsed.configure(state="disabled")

    def _save_pem_files(self, text):
        """把憑證鏈裡的每張憑證存成 cert0.pem、cert1.pem …,回傳要顯示在解析結果裡的列。"""
        try:
            ders = extract_spdm_certs(text)
        except Exception as e:
            return [("■ 憑證檔案 (.pem)\n", "hdr"), (f"  解析憑證失敗:{e}\n", "err")]
        if not ders:
            return []
        rows = [("■ 憑證檔案 (.pem)\n", "hdr")]
        folder = self.vars["pem_dir"].get().strip()
        if not folder:
            return rows + [("  ✘ 尚未設定憑證輸出資料夾,沒有儲存。\n", "err")]
        try:
            os.makedirs(folder, exist_ok=True)
            for i, der in enumerate(ders):
                path = os.path.join(folder, f"cert{i}.pem")
                with open(path, "w", encoding="ascii", newline="\n") as f:
                    f.write(der_to_pem(der))
                rows.append((f"  ✔ {path}\n", "ok"))
        except OSError as e:
            rows.append((f"  ✘ 儲存失敗:{e}\n", "err"))
        return rows


def _fmt_secs(sec):
    """秒數轉成易讀格式:12.3 s 或 1 min 05.2 s"""
    if sec < 60:
        return f"{sec:.1f} s"
    m, s = divmod(sec, 60)
    return f"{int(m)} min {s:04.1f} s"


class FlashGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TS18xx Utilities Tool")
        self.geometry("820x780")
        self.minsize(780, 640)

        self.proc = None
        self.out_q = queue.Queue()
        self._cr_pending = False  # 上次收到 \r,下一段文字要覆蓋最後一行

        # 依序燒錄的狀態
        self.jobs = []          # 尚未執行的 (flash_index, cs)
        self.job_total = 0
        self.job_results = []   # [((fi, cs), ok, code)]
        self.cur = None
        self.cancelled = False

        self._raw = {}
        self.vars = {k: tk.StringVar(value=v) for k, v in DEFAULTS.items()}
        self.sel = {c: tk.BooleanVar(value=(c == (0, 0))) for c in COMBOS}
        self.verify = tk.BooleanVar(value=False)   # --verify
        self.smart = tk.BooleanVar(value=False)    # --smart
        self.job_start = None                      # 目前項目的開始時間
        self.job_times = {}                        # {(fi, cs): 秒}
        self._load_settings()

        # 分頁:選中的分頁字體加大加粗
        style = ttk.Style(self)
        style.configure("TNotebook.Tab", padding=(12, 4))
        style.map("TNotebook.Tab",
                  font=[("selected", ("", 12, "bold"))],
                  padding=[("selected", (14, 6))])
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        tab_flash = ttk.Frame(self.nb)
        self.nb.add(tab_flash, text="SPI Flash 燒錄")
        self._build_ui(tab_flash)
        self.tflx = TflxProvTab(self.nb, self._raw, self._save_settings)
        self.nb.add(self.tflx, text="TFLX Provisioning")
        self.spdm = SpdmLogTab(self.nb, self._raw, self._save_settings)
        self.nb.add(self.spdm, text="SPDM Log Paser")

        for v in list(self.vars.values()) + list(self.sel.values()) + [self.verify, self.smart]:
            v.trace_add("write", lambda *_: (self._update_preview(), self._save_settings()))
        self._update_preview()
        self.after(100, self._poll_output)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self, root):
        pad = {"padx": 6, "pady": 5}

        f = ttk.LabelFrame(root, text="路徑")
        f.pack(fill="x", padx=10, pady=(10, 4))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="ts18xx_ftdi_flash").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["exe"]).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_exe).grid(row=0, column=2, **pad)

        ttk.Label(f, text="spi_image 資料夾").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["bin_dir"]).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_bin_dir).grid(row=1, column=2, **pad)

        # 六個 Checkbutton
        f_sel = ttk.LabelFrame(root, text="要燒錄的項目(勾選的會依序燒錄)")
        f_sel.pack(fill="x", padx=10, pady=4)
        f_sel.columnconfigure(1, weight=1, uniform="cs")
        f_sel.columnconfigure(2, weight=1, uniform="cs")
        self.chks = []
        self.combo_labels = {}
        # 左邊:--flash_index(每一列);右邊:--cs 0 / --cs 1(兩欄)
        for fi in (0, 1, 2):
            ttk.Label(f_sel, text=f"--flash_index {fi}", font=("", 10, "bold")).grid(
                row=fi, column=0, sticky="w", padx=(10, 14), pady=4)
            for cs in (0, 1):
                cell = ttk.Frame(f_sel)
                cell.grid(row=fi, column=1 + cs, sticky="w", padx=6, pady=3)
                chk = ttk.Checkbutton(cell, text=f"--cs {cs}", variable=self.sel[(fi, cs)])
                chk.pack(anchor="w")
                lab = ttk.Label(cell, text="")
                lab.pack(anchor="w", padx=(24, 0))
                self.chks.append(chk)
                self.combo_labels[(fi, cs)] = lab
        f_selbtn = ttk.Frame(f_sel)
        f_selbtn.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 6))
        self.btn_all = ttk.Button(f_selbtn, text="全選", command=self._select_found)
        self.btn_all.pack(side="left")
        self.btn_none = ttk.Button(f_selbtn, text="全不選", command=lambda: self._set_all(False))
        self.btn_none.pack(side="left", padx=(6, 0))

        # SPI 參數
        f_opt = ttk.LabelFrame(root, text="燒錄參數")
        f_opt.pack(fill="x", padx=10, pady=4)
        ttk.Label(f_opt, text="SPI Speed (MHz) --speed").grid(row=0, column=0, sticky="w", padx=(10, 6), pady=(6, 0))
        ttk.Label(f_opt, text="Bus Width --bus_width").grid(row=0, column=1, sticky="w", padx=6, pady=(6, 0))
        self.cb_speed = ttk.Combobox(f_opt, textvariable=self.vars["speed"], values=FLASH_SPEEDS,
                                     state="readonly", width=12)
        self.cb_speed.grid(row=1, column=0, sticky="w", padx=(10, 6), pady=(2, 6))
        self.cb_bus = ttk.Combobox(f_opt, textvariable=self.vars["bus_width"], values=FLASH_BUS_WIDTHS,
                                   state="readonly", width=12)
        self.cb_bus.grid(row=1, column=1, sticky="w", padx=6, pady=(2, 6))
        self.chk_verify = ttk.Checkbutton(f_opt, text="Verify (--verify)", variable=self.verify)
        self.chk_verify.grid(row=1, column=2, sticky="w", padx=(20, 6), pady=(2, 6))
        self.chk_smart = ttk.Checkbutton(f_opt, text="Smart (only changed sectors) (--smart)", variable=self.smart)
        self.chk_smart.grid(row=1, column=3, sticky="w", padx=6, pady=(2, 6))

        ttk.Label(root, foreground="gray",
                  text="固定參數:--target ext  --spi_mode 0  program"
                  ).pack(anchor="w", padx=14, pady=(0, 2))

        # 指令預覽
        f_prev = ttk.LabelFrame(root, text="將依序執行的指令")
        f_prev.pack(fill="x", padx=10, pady=4)
        f_prev.columnconfigure(0, weight=1)
        self.preview = tk.Text(f_prev, height=5, wrap="word", state="disabled",
                               background=self.cget("background"), relief="flat")
        pys = ttk.Scrollbar(f_prev, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pys.set)
        self.preview.grid(row=0, column=0, sticky="ew", padx=(6, 0), pady=4)
        pys.grid(row=0, column=1, sticky="ns", pady=4)
        ttk.Button(f_prev, text="複製指令", command=self._copy_cmd).grid(
            row=1, column=0, columnspan=2, sticky="e", padx=6, pady=(0, 4))

        # 按鈕
        f_btn = ttk.Frame(root)
        f_btn.pack(fill="x", padx=10, pady=4)
        self.btn_run = ttk.Button(f_btn, text="▶ 開始燒錄", command=self.run)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(f_btn, text="■ 停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(f_btn, text="清除輸出", command=self._clear_log).pack(side="left")
        self.status = tk.StringVar(value="就緒")
        ttk.Label(f_btn, textvariable=self.status).pack(side="right")

        # 輸出
        f_log = ttk.LabelFrame(root, text="輸出")
        f_log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        f_log.rowconfigure(0, weight=1)
        f_log.columnconfigure(0, weight=1)
        self.log = tk.Text(f_log, wrap="none", state="disabled", font=("Consolas", 10))
        ys = ttk.Scrollbar(f_log, orient="vertical", command=self.log.yview)
        xs = ttk.Scrollbar(f_log, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.log.tag_configure("err", foreground="#c0392b")
        self.log.tag_configure("ok", foreground="#1e8449")
        self.log.tag_configure("info", foreground="#2471a3")

    def _set_all(self, value):
        for v in self.sel.values():
            v.set(value)

    def _select_found(self):
        """全選:只勾選資料夾內找得到 .bin 檔的項目,找不到的取消勾選。"""
        if not self.vars["bin_dir"].get().strip():
            self.status.set("請先選擇 spi_image 資料夾")
            return
        n = 0
        for (fi, cs), var in self.sel.items():
            found = Path(self.bin_path(fi, cs)).is_file()
            var.set(found)
            n += found
        self.status.set(f"已勾選 {n} 個找到檔案的項目")

    def _set_running(self, running):
        self.btn_run.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        state = "disabled" if running else "normal"
        for w in self.chks + [self.btn_all, self.btn_none, self.chk_verify, self.chk_smart]:
            w.configure(state=state)
        for w in (self.cb_speed, self.cb_bus):
            w.configure(state="disabled" if running else "readonly")

    # ---------- 指令組合 ----------
    def selected_combos(self):
        return [c for c in COMBOS if self.sel[c].get()]

    def bin_name(self, fi, cs):
        """檔名依 flash_index 與 cs 自動決定:spi_image_port_{flash_index}_comp_{cs}.bin"""
        return f"spi_image_port_{fi}_comp_{cs}.bin"

    def bin_path(self, fi, cs):
        d = self.vars["bin_dir"].get().strip()
        return str(Path(d) / self.bin_name(fi, cs)) if d else self.bin_name(fi, cs)

    def build_cmd(self, fi, cs):
        cmd = [self.vars["exe"].get().strip(),
               "--target", "ext", "--flash_index", str(fi),
               "--speed", self.vars["speed"].get(), "--spi_mode", "0",
               "--bus_width", self.vars["bus_width"].get(),
               SUBCOMMAND, "--bin", self.bin_path(fi, cs), "--cs", str(cs)]
        if self.verify.get():
            cmd.append("--verify")
        if self.smart.get():
            cmd.append("--smart")
        return cmd

    def all_cmdlines(self):
        return [subprocess.list2cmdline(self.build_cmd(fi, cs)) for fi, cs in self.selected_combos()]

    def _update_bin_status(self):
        has_dir = bool(self.vars["bin_dir"].get().strip())
        for (fi, cs), lab in self.combo_labels.items():
            name = self.bin_name(fi, cs)
            checked = self.sel[(fi, cs)].get()
            if not has_dir:
                lab.configure(text=name, foreground="gray")
            elif Path(self.bin_path(fi, cs)).is_file():
                lab.configure(text=f"{name}  ✔ 找到", foreground="#1e8449")
            else:
                lab.configure(text=f"{name}  ✘ 找不到",
                              foreground="#c0392b" if checked else "gray")

    def _update_preview(self):
        self._update_bin_status()
        lines = self.all_cmdlines()
        text = "\n".join(f"[{i}] {c}" for i, c in enumerate(lines, 1)) if lines else "(尚未勾選任何項目)"
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(self.all_cmdlines()))
        self.status.set("已複製指令")

    # ---------- 檔案選擇 ----------
    def _browse_exe(self):
        init = os.path.dirname(self.vars["exe"].get()) or None
        p = filedialog.askopenfilename(title="選擇 ts18xx_ftdi_flash", initialdir=init,
                                       filetypes=[("可執行檔", "*.exe *.bat *.cmd"), ("所有檔案", "*.*")])
        if p:
            self.vars["exe"].set(p)

    def _browse_bin_dir(self):
        init = self.vars["bin_dir"].get().strip() or None
        if init and not os.path.isdir(init):
            init = None
        p = filedialog.askdirectory(title="選擇 spi_image_port_x_comp_x.bin 所在資料夾", initialdir=init)
        if p:
            self.vars["bin_dir"].set(os.path.normpath(p))

    # ---------- 依序燒錄 ----------
    def run(self):
        if self.proc is not None or self.jobs:
            return
        combos = self.selected_combos()
        if not combos:
            messagebox.showwarning("尚未勾選", "請至少勾選一個要燒錄的項目。")
            return
        if not self.vars["bin_dir"].get().strip():
            messagebox.showwarning("缺少參數", "請先選擇 spi_image 所在的資料夾。")
            return
        missing = [self.bin_path(fi, cs) for fi, cs in combos if not Path(self.bin_path(fi, cs)).is_file()]
        if missing:
            messagebox.showerror("找不到檔案", "資料夾內缺少以下檔案,尚未開始燒錄:\n\n" + "\n".join(missing))
            return

        self.jobs = list(combos)
        self.job_total = len(combos)
        self.job_results = []
        self.job_times = {}
        self.cancelled = False
        self._set_running(True)
        self._next_job()

    def _next_job(self):
        fi, cs = self.jobs.pop(0)
        self.cur = (fi, cs)
        idx = self.job_total - len(self.jobs)
        cmd = self.build_cmd(fi, cs)
        self._append(f"\n===== [{idx}/{self.job_total}] flash_index {fi}, cs {cs} =====\n", "info")
        self._append(f"> {subprocess.list2cmdline(cmd)}\n", "info")
        self.status.set(f"燒錄中 ({idx}/{self.job_total}):flash {fi} / cs {cs}")

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.job_start = time.monotonic()
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, env=_stream_env(), creationflags=flags)
        except FileNotFoundError:
            self._append("找不到 ts18xx_ftdi_flash,請確認路徑或 PATH 設定。\n", "err")
            self.job_results.append(((fi, cs), False, None))
            self._all_done()
            return
        except Exception as e:
            self._append(f"啟動失敗:{e}\n", "err")
            self.job_results.append(((fi, cs), False, None))
            self._all_done()
            return

        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            text = dec.decode(chunk)
            if text:
                self.out_q.put(("data", text))
        proc.wait()
        self.out_q.put(("done", proc.returncode))

    def _poll_output(self):
        try:
            while True:
                kind, data = self.out_q.get_nowait()
                if kind == "data":
                    self._append_stream(data)
                else:
                    self._finish(data)
        except queue.Empty:
            pass
        self.after(100, self._poll_output)

    def _finish(self, code):
        """目前這一個項目結束:成功就接著燒下一個,失敗或被停止就中止後面的。"""
        self.proc = None
        self._end_stream_line()
        ok = (code == 0)
        self.job_results.append((self.cur, ok, code))
        elapsed = time.monotonic() - self.job_start if self.job_start else None
        if elapsed is not None:
            self.job_times[self.cur] = elapsed
        t = f",耗時 {_fmt_secs(elapsed)}" if elapsed is not None else ""
        if ok:
            self._append(f"[完成] 結束碼 0{t}\n", "ok")
        else:
            self._append(f"[失敗] 結束碼 {code}{t}\n", "err")
        if ok and not self.cancelled and self.jobs:
            self._next_job()
        else:
            self._all_done()

    def _all_done(self):
        self.proc = None
        self._set_running(False)
        skipped = list(self.jobs)
        self.jobs = []
        failed = [r for r in self.job_results if not r[1]]

        lines = ["", "===== 結果 ====="]
        for (fi, cs), ok, code in self.job_results:
            t = self.job_times.get((fi, cs))
            ts = f"  耗時 {_fmt_secs(t)}" if t is not None else ""
            lines.append(f"✔ flash {fi} / cs {cs}{ts}" if ok
                         else f"✘ flash {fi} / cs {cs}(結束碼 {code}){ts}")
        for fi, cs in skipped:
            lines.append(f"- flash {fi} / cs {cs}(未執行)")
        if self.job_times:
            lines.append(f"總耗時 {_fmt_secs(sum(self.job_times.values()))}")
        all_ok = not failed and not skipped
        self._append("\n".join(lines) + "\n", "ok" if all_ok else "err")

        if all_ok:
            self.status.set(f"全部完成 ({len(self.job_results)}/{self.job_total})")
        elif self.cancelled:
            self.status.set("已停止")
        else:
            fi, cs = failed[0][0]
            self.status.set(f"失敗:flash {fi} / cs {cs}")

    def stop(self):
        self.cancelled = True
        if self.proc is not None:
            self.proc.terminate()
            self._append("[已要求停止,後面的項目不會執行]\n", "err")

    # ---------- 輸出 ----------
    def _append(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _append_stream(self, text):
        """處理子程序輸出:\\r 代表回到行首,下一段文字覆蓋同一行(像 cmd 一樣)。"""
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)  # 移除 ANSI 控制碼
        self.log.configure(state="normal")
        for tok in re.split(r"(\r\n|\r|\n)", text):
            if not tok:
                continue
            if tok == "\r":
                self._cr_pending = True
            elif tok in ("\n", "\r\n"):
                self.log.insert("end", "\n")
                self._cr_pending = False
            else:
                if self._cr_pending:
                    self.log.delete("end-1c linestart", "end-1c")
                    self._cr_pending = False
                self.log.insert("end", tok)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _end_stream_line(self):
        self._cr_pending = False
        self.log.configure(state="normal")
        if self.log.get("end-2c", "end-1c") not in ("\n", ""):
            self.log.insert("end", "\n")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---------- 設定存取 ----------
    def _load_settings(self):
        data = None
        for path in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                continue
        if not isinstance(data, dict):
            return
        self._raw = data
        # 舊版存的是完整 .bin 路徑,轉成資料夾
        if "bin_dir" not in data and data.get("bin"):
            data["bin_dir"] = os.path.dirname(data["bin"])
        for k in DEFAULTS:
            if isinstance(data.get(k), str):
                self.vars[k].set(data[k])
        if self.vars["speed"].get() not in FLASH_SPEEDS:
            self.vars["speed"].set(DEFAULTS["speed"])
        if self.vars["bus_width"].get() not in FLASH_BUS_WIDTHS:
            self.vars["bus_width"].set(DEFAULTS["bus_width"])
        self.verify.set(bool(data.get("verify", False)))
        self.smart.set(bool(data.get("smart", False)))

        # 勾選狀態;舊版只有單一 flash_index / cs,就轉成只勾那一個
        selected = data.get("selected")
        if selected is None and ("flash_index" in data or "cs" in data):
            selected = [f"{data.get('flash_index', '0')},{data.get('cs', '0')}"]
        if isinstance(selected, list):
            chosen = {str(s) for s in selected}
            for (fi, cs), var in self.sel.items():
                var.set(f"{fi},{cs}" in chosen)

    def _save_settings(self):
        data = {k: v.get() for k, v in self.vars.items()}
        data["selected"] = [f"{fi},{cs}" for (fi, cs) in COMBOS if self.sel[(fi, cs)].get()]
        data["verify"] = self.verify.get()
        data["smart"] = self.smart.get()
        tflx = getattr(self, "tflx", None)
        if tflx is not None:
            data["tflx_exe"] = tflx.vars["exe"].get()
            data["tflx_img"] = tflx.vars["img"].get()
        spdm = getattr(self, "spdm", None)
        if spdm is not None:
            for k in ("exe", "iface", "port", "slot", "version", "pem_dir", "save_pem"):
                data[f"spdm_{k}"] = spdm.vars[k].get()
        try:
            SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        busy = (self.proc is not None or self.tflx.proc is not None
                or self.spdm.proc is not None)
        if busy:
            if not messagebox.askyesno("確認", "仍有程序在執行,確定要離開嗎?"):
                return
            self.cancelled = True
            if self.proc is not None:
                self.proc.terminate()
            self.tflx.terminate()
            self.spdm.terminate()
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    FlashGui().mainloop()
