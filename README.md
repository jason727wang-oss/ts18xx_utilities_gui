# ts18xx_utilities_gui

TS18xx 開發板常用命令列工具的圖形介面（Tkinter）。把 `ts18xx_ftdi_flash`、`ts18xx_tflx_prov` 與 `ts18xx_spdm` 的固定參數包起來，只要選檔案、勾選項目、按按鈕，不用每次手打指令。

目前版本：**1.0.4**

## 功能

程式有三個分頁：

### 1. SPI Flash 燒錄

呼叫 `ts18xx_ftdi_flash`，把 `.bin` 映像檔燒錄到外部 SPI Flash。

- 可勾選 `--flash_index`（0 / 1 / 2）× `--cs`（0 / 1），共 6 種組合，勾選的項目會**依序**燒錄。
- 只需選擇 `spi_image` 資料夾，檔名依組合自動決定：`spi_image_port_{flash_index}_comp_{cs}.bin`。
- 每個組合旁會顯示該檔案是否存在（✔ 找到 / ✘ 找不到）。
- 「全選」只會勾選資料夾內實際找得到檔案的項目。
- 開始前會檢查：是否有勾選、是否已選資料夾、檔案是否齊全，缺少任何一項就不會開始。
- 任一項目失敗（結束碼非 0）或按下停止，後面的項目就不會執行，最後會列出每個組合的結果。
- 「燒錄參數」區塊可選擇：
  - SPI Speed（MHz）→ `--speed`，可選 12 / 15 / 20 / 40（預設 12）。
  - Bus Width → `--bus_width`，可選 single / dual / quad（預設 single）。
  - Verify → 勾選時加上 `--verify`。
  - Smart (only changed sectors) → 勾選時加上 `--smart`。
- 按鈕旁的「動作」選單可切換要執行的子命令，按鈕為「▶ 執行 Cmd」：
  - `program`：燒錄（預設），行為如上。
  - `scan`：執行 `... --bus_width <寬度> scan`，不需要 `.bin` 檔。
  - `set_qe`：執行 `... --bus_width <寬度> set_qe --cs all`，不需要 `.bin` 檔。
  - `scan` / `set_qe` 只會執行上面那一條指令（固定 `--flash_index 0`，執行一次），不受下方勾選項目、Verify、Smart 影響；Speed 與 Bus Width 仍然有效。
- 每一顆燒錄結束後會顯示該顆的**耗時**，最後的結果列表也會列出各顆耗時與總耗時。耗時是從啟動 `ts18xx_ftdi_flash` 到程序結束的實際時間，包含 erase 與 write；若勾選 Verify，也會包含驗證時間。
- 固定參數：`--target ext --spi_mode 0 program`

實際執行的指令（以 `flash_index 0`、`cs 0` 為例）：

```
ts18xx_ftdi_flash --target ext --flash_index 0 --speed 12 --spi_mode 0 --bus_width single program --bin <spi_image 資料夾>\spi_image_port_0_comp_0.bin --cs 0 [--verify] [--smart]
```

### 2. TFLX Provisioning

呼叫 `ts18xx_tflx_prov.exe`，提供三個動作：

| 按鈕 | 說明 | 對應指令 |
| --- | --- | --- |
| ⚡ Start Provisioning | 把 `ap_otp.bin` 寫入 TFLX STASH | `write --img_in <ap_otp.bin> --stash` |
| ↻ Stash Load + Exit | 載入 stash 並離開 provisioning mode | `exit stash` |
| ⌫ Stash Clear | 清除 stash | `cmd --stash_clear` |

- 固定參數：`--iface_type spi -i 0`（FTDI SPI，channel 0）。
- **執行任何動作前，請先按下 EVB 的 RESET**，讓裝置進入 provisioning mode；若輸出出現 `RW_ERROR`，通常就是忘了這一步。
- Start Provisioning 時工具會詢問是否更新韌體 `[y/N]`，GUI 會自動回答 `n`。若要在命令列手動執行，需用管線送入：`echo n | ...`。
- 這個工具失敗時結束碼仍可能是 0，所以 GUI 會再檢查輸出文字（例如 `bytes match`、`left provisioning mode`、`Restoring OTP values`、`erased from on-device flash`）來判斷成功或失敗。

### 3. SPDM Log Paser

呼叫 `ts18xx_spdm.exe` 讀取裝置的 SPDM 資訊，並把輸出整理成容易閱讀的結果（分頁名稱沿用 `Paser` 的拼法）。

**路徑與選項**

| 介面上的選項 | 對應參數 | 預設 |
| --- | --- | --- |
| Interface（I2C / SPI / I3C） | `--iface_type`（`i2c` / `spi` / `i3c`） | SPI |
| SPT Channel | `--port`（非負整數） | 0 |
| Cert Chain Slot（0–7） | `--slot_index` | 0 |
| SPDM Version（1.1 / 1.4） | `--spdm_version` | 1.4 |

- `ts18xx_spdm.exe` 路徑欄位預設為 `ts18xx_spdm.exe`（在 PATH 內可直接使用），也可用「選擇…」指定。
- 「對應的指令」區塊會即時顯示實際要執行的指令。
- 選到 **I2C** 時，輸出視窗會提示「需使用 Aardvark I2C Port」；切換成 SPI 或 I3C 時，這些提示會被清除。
- 「Enable Secure Session」尚未支援（還不確定對應的命令列參數）。
- 按 **▶ Run** 執行，**■ 停止** 中止，**清除輸出** 同時清空兩個視窗。成功與否**只看結束碼**（0 為成功）。

**兩個輸出視窗**（中間的分隔線可拖曳調整大小）

- **輸出**：工具的原始輸出，即時顯示。
- **解析結果**：執行結束後自動產生，包含：
  - 整體結果（PASS / FAIL）與各驗證項目（✔ / ✘）。
  - 連線資訊、Responder 傳輸大小與 Chunking 設定。
  - 憑證：憑證鏈總長度、每張憑證的驗證結果、Root Certificate Hash、Cert Chain Digest。
  - **憑證鏈結構**：DSP0274 的結構說明，加上從 log 位元組解析出的實際資料（Length、Reserved、RootHash，以及每張憑證的位置、大小、Subject、Issuer、有效期間、**SHA-384**），並重新計算整條鏈的 SHA-384，與裝置回報的 Cert Chain Digest 比對。
  - 簽章驗證結果（Challenge、Measurements）。
  - 量測區塊（Block 1–N 的類型、大小與內容，全為 0 的會標示，過長的內容會截短）。
  - BMC / GPIO 的量測訊息。
- FAIL 的判斷（程式實作如此，尚未用真實的失敗 log 驗證）：結束碼不為 0、輸出出現 `[ERROR]` / `[WARNING]`，或驗證訊息含 fail、error、mismatch、invalid、not verified 等字樣。

**憑證存成 .pem**

- 勾選「解析時將憑證存成 cert0.pem、cert1.pem …」（預設勾選），解析時會把憑證鏈裡的每張憑證存成 PEM（`-----BEGIN CERTIFICATE-----` 開頭的文字格式）。
- 存檔資料夾預設與 `ts18xx_utilities_gui.py` 在同一個資料夾，可在「憑證輸出資料夾」欄位更改。
- 檔名為 `cert0.pem`、`cert1.pem` …，依憑證鏈順序命名（cert0 為根憑證）。**同一資料夾內每次執行都會覆蓋同名檔案**。
- 存檔結果會列在「解析結果」最下方的「憑證檔案 (.pem)」。
- `.gitignore` 已排除 `*.pem` 與 `*.der`，避免誤 commit。

只使用 Python 標準函式庫；憑證的 Subject、Issuer、有效期間是用程式內建的簡易 DER 解析讀出的。

### 共通功能

- 指令預覽。
- 即時顯示工具輸出，支援 `\r` 覆蓋同一行（進度條），並移除 ANSI 控制碼。
- 輸出強制使用 UTF-8（`PYTHONUTF8=1`），避免 cp950 造成亂碼。
- 可隨時停止；關閉視窗時若仍有程序在執行，會先詢問確認。
- 設定自動儲存。

## 環境需求

- Windows
- Python 3（含 Tkinter，官方安裝程式預設就有）
- 沒有任何第三方套件相依
- 已安裝 `ts18xx_ftdi_flash`、`ts18xx_tflx_prov.exe` 與 `ts18xx_spdm.exe`（可放在 PATH，或在 GUI 內指定路徑）
- 使用 SPDM 分頁的 I2C 介面時，需要 Aardvark I2C 轉接器

## 使用方式

1. 下載或 clone 此專案。
2. 雙擊 `run_ts18xx_gui.bat`。
   - 會優先使用 `py` 啟動器，找不到才改用 `python`。
   - 以有主控台的方式執行，啟動錯誤訊息不會一閃而過。
   - `.bat` 必須與 `ts18xx_utilities_gui.py` 放在同一個資料夾。

也可以直接在命令列執行：

```
python ts18xx_utilities_gui.py
```

更新程式後，需要關掉視窗重新啟動，新版才會生效。

## 設定檔

設定存放在使用者家目錄：

```
%USERPROFILE%\.ts18xx_utilities_gui.json
```

內容包含：

- Flash 燒錄：`ts18xx_ftdi_flash` 路徑、`spi_image` 資料夾、已勾選的組合、Speed、Bus Width、Verify、Smart。
- TFLX Provisioning：`ts18xx_tflx_prov.exe` 與 `ap_otp.bin` 路徑。
- SPDM Log Paser：`ts18xx_spdm.exe` 路徑、Interface、SPT Channel、Cert Chain Slot、SPDM Version、憑證輸出資料夾與是否存檔。

舊版放在程式旁邊的 `ts18xx_utilities_gui_settings.json` 仍會讀取一次並自動轉換。

## 預設路徑

TFLX Provisioning 分頁的預設路徑寫在 `ts18xx_utilities_gui.py` 開頭的 `TFLX_DEFAULT_EXE` 與 `TFLX_DEFAULT_IMG`，是作者電腦上的路徑。換電腦或換使用者時，請在 GUI 內用「選擇…」重新指定（會自動記住），或直接修改這兩個常數。

## 檔案說明

| 檔案 | 說明 |
| --- | --- |
| `ts18xx_utilities_gui.py` | 主程式 |
| `run_ts18xx_gui.bat` | Windows 啟動檔 |
| `.gitignore` | 排除解析時產生的憑證檔（`*.pem`、`*.der`） |

---

## SPDM 筆記

以下是開發 SPDM Log Paser 時，用 TS1800-B1 實測與討論的重點整理。標示「**推測**」的項目沒有被官方文件或實驗證實，「**未確認**」的項目請參考最後一節。

### 1. 憑證鏈的結構（SPDM 規格 DSP0274 的 Certificate chain format）

| Offset | Size | Field | 說明 |
| --- | --- | --- | --- |
| 0 | 2 | Length | 整個結構的總長度（含 Length、Reserved、RootHash 與所有憑證） |
| 2 | 2 | Reserved | 保留，值為 0 |
| 4 | H | RootHash | 根憑證（DER）的雜湊；H 為雜湊長度，SHA-384 為 48 |
| 4+H | 其餘 | Certificates | DER 編碼的 X.509 v3 憑證，由根憑證依序排到最末端憑證 |

實測的鏈使用 SHA-384（H = 48），共 5 張憑證：cert0 為 595 bytes、cert1 為 632 bytes、cert2 約 651～653 bytes、cert3 為 688 bytes、cert4 約 1147～1148 bytes。cert2 與 cert4 的長度會差 1 byte，是因為 ECDSA 簽章的 DER 長度每次可能不同。

### 2. Cert Chain Digest 與 Root Certificate Hash 怎麼算

- **Cert Chain Digest** = SHA-384（整個憑證鏈結構，從 Length 欄位開始，含 Length、Reserved、RootHash 與所有憑證）。
  - 實測重算的結果與裝置回報的值一致。
  - 只對憑證本體（不含檔頭與 RootHash）算，或改用 SHA3-384，都不一致。
- **Root Certificate Hash** = SHA-384（cert0 的 DER，595 bytes），這個值被放在憑證鏈檔頭的 RootHash 欄位。

### 3. 各項驗證用什麼驗

| 驗證項目 | 做法 | 用到的金鑰 |
| --- | --- | --- |
| Cert Chain Digest | 對整條憑證鏈重算 SHA-384，與裝置回報的值比對 | 沒有，只有雜湊 |
| Root Certificate Hash | 對 cert0 重算雜湊，與鏈檔頭的 RootHash 比對 | 沒有，只有雜湊 |
| cert1～cert4 的 ECDSA verification | 各自用**簽發者**憑證的公鑰驗證簽章（cert4 用 cert3 的公鑰） | 簽發者的公鑰 |
| Challenge / Measurements Signature Verification | 驗證裝置回應的簽章 | 葉子憑證（cert4）的公鑰（依 SPDM 一般做法，**未實際重算驗證**） |

- **`Cert Chain Digest Verified!` 只是比較兩個雜湊值是否相同**，沒有用到任何公鑰。它能證明「拿到的鏈沒有損壞、沒有被換掉」，但不能單獨證明鏈或裝置可信；可信度來自逐層的簽章驗證與 Challenge 的簽章。
- log 中這句話會出現兩次：一次是比對 Get Digest 回報的值，另一次是比對 Challenge 回應裡附帶的雜湊。Challenge 回應開頭確實帶著同一個 48 bytes 的 Cert Chain Digest。簽章是否涵蓋這個值，屬於**推測**（沒有逐欄位解析封包）。

### 4. 這條鏈各憑證的角色

| 憑證 | CN | 簽發者 | Key Usage | CA |
| --- | --- | --- | --- | --- |
| cert0 | CPG Root CA 1 | 自簽 | 未檢查 | 未檢查 |
| cert1 | ProvisionerCA | CPG Root CA 1 | 未檢查 | 未檢查 |
| cert2 | TS1800-B1 IDevID CA | ProvisionerCA | Certificate Sign | CA:TRUE |
| cert3 | TS1800-B1 LDevID CA | IDevID CA | Certificate Sign | CA:TRUE |
| cert4 | TS1800-B1 FMC Alias LF | LDevID CA | Digital Signature | 非 CA |

- **LDevID 在這條鏈裡是中間的 CA，不是葉子**。IEEE 802.1AR 的 LDevID 通常是終端憑證，但實際角色要看憑證本身的屬性，不能只看名稱。
- **cert4（FMC Alias，頁面上稱 AliasFMC End）才是葉子**：Key Usage 為 Digital Signature、不是 CA，並帶有兩個 DICE 擴充欄位（OID `2.23.133.5.4.1`，推測是韌體量測資訊）。
- DICE 分層的通用概念：IDevID 是出廠決定、長期不變的身分；LDevID 是裝置在本地派生出的一層身分；Alias（FMC Alias）則針對目前載入的韌體（First Mutable Code）派生，所以韌體改變，Alias 金鑰與憑證就會跟著變，放在最末端當葉子。

### 5. 用 OpenSSL 驗 cert4 為什麼會失敗

直接用 `openssl verify` 驗 cert4 會出現兩個錯誤，**都不是憑證或簽章損壞**：

1. **`error 9: certificate is not yet valid`**：cert4 的有效期間是 2035-12-31 ～ 2063-12-31，現在還沒到起始日（見下一節）。
2. **`error 34: unhandled critical extension`**：cert4 有 OpenSSL 不認得、且被標成 critical 的 DICE 擴充欄位（OID `2.23.133.5.4.1`），依 X.509 規則驗證端必須拒絕。

加上兩個選項後可通過（只加其中一個仍會失敗）：

```
openssl verify -partial_chain -no_check_time -ignore_critical -CAfile cert3.pem cert4.pem
```

整條鏈一次驗（以 cert0 為信任錨，其餘當中繼憑證；Windows 可用 `copy /b cert1.pem+cert2.pem+cert3.pem mid.pem` 串接）：

```
openssl verify -no_check_time -ignore_critical -CAfile cert0.pem -untrusted mid.pem cert4.pem
```

注意：`-ignore_critical` 代表放棄檢查 DICE 擴充欄位的內容，量測值是否合理要靠 SPDM 的 Measurements 去確認。簽章鏈本身（逐層 ECDSA）仍然會被驗證。

### 6. cert4 的起始日為什麼是 2035-12-31

**未確認，原因不明。** 已知的事實：

- cert4 的 notBefore 是 2035-12-31、notAfter 是 2063-12-31（編碼為 UTCTime `351231000000Z`，依 RFC 5280 解讀為 2035 年）。
- cert1～cert3 的 notAfter 都是 9999-12-31，這是規格建議的「沒有定義到期日」寫法（因為多數硬體沒有可靠的時鐘）。
- cert4 的日期不符合這種常見做法，看起來是韌體自行選定的值；沒有找到 Microchip 針對 TS1800 的公開說明。
- 可以換開機或換板子比較 cert4 的日期是否改變，或直接詢問 Microchip。

### 7. `cert_chain_int_flash.bin` 與 cert0～cert4 的對應

`cert_chain_int_flash.bin` 共 3072 bytes，切成三個 1024 bytes 的區塊，每個區塊放一張 DER 憑證，其餘補 `0x00`。它**不是** SPDM 憑證鏈結構（沒有 Length、Reserved、RootHash 檔頭），順序也與 SPDM 鏈相反（由末端排到根）。

| .bin 位置 | 內容 | 與 cert0～cert4 比對 |
| --- | --- | --- |
| 0 | TS1800-B1 IDevID CA（653 bytes） | 對應 cert2：內容相同，**簽章不同** |
| 1024 | ProvisionerCA（632 bytes） | 與 cert1 逐 byte 相同 |
| 2048 | CPG Root CA 1（595 bytes） | 與 cert0 逐 byte 相同 |

- **cert3（LDevID CA）與 cert4（FMC Alias）不在 .bin 裡**。
- cert2 在 .bin、你每次讀回的鏈裡，Subject、Issuer、序號、有效期間、公鑰都相同，只有簽章位元組不同；兩個版本都由 cert1 驗證為有效。ECDSA 每次簽章本來就不同，所以代表 **cert2（IDevID CA）會被重新簽發**；cert3、cert4 跨次執行簽章也不同。
- 依工具「Attestation」頁面上的說明：
  - **Read Device Certs**：Regenerates certs on-device and reads them over FTDI SPI, then builds the chain（在裝置上重新產生憑證，經 FTDI SPI 讀出後建立憑證鏈）。
  - **Program Certs to Device**：把建好的憑證鏈寫進裝置的 internal flash（TFLX provisioning，`write_cert_chain`）。
  - **ROM Certificates (fixed)**：IDevID 為 ROM-generated、signed、stored in flash；LDevID 為 ROM-generated、in SRAM；AliasFMC End 為 device leaf cert (ROM)。
  - Private Key 欄位標示為 `optional — ProvAuth`，是 provisioning 授權用的金鑰。
- 因此：.bin 只有 IDevID CA、ProvisionerCA、Root 三張，是因為只有它們存在 flash；LDevID、AliasFMC End 不存在 flash 裡。**推測**：按下 Read Device Certs 時裝置重新產生憑證，是 IDevID CA 簽章改變的原因。

想確認簽章是在哪一步改變，可以依序跑一次 SPDM 記下 cert2 的 SHA-384、只按 Read Device Certs 後再跑一次、再按 Program Certs to Device 後再跑一次，比對各次的 cert2。

### 8. 尚待確認

- cert4 起始日為 2035-12-31 的原因。
- IDevID CA（cert2）每次由誰、用哪把私鑰簽發。
- 工具頁面的 Signer CA 與 cert1（ProvisionerCA）是否是同一個角色。
- 「Enable Secure Session」對應的 `ts18xx_spdm` 命令列參數（尚未實作）。
- 解析結果的 FAIL 判斷，需要用真實的失敗 log 驗證。
- I2C / I3C 的 `--iface_type` 參數值（`i2c` / `i3c`）是否被工具接受。
