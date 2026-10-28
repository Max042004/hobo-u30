# HOBO U30 Raspberry Pi Station

用 Raspberry Pi 的 USB HID 直接讀取 HOBO U30，保存至 SQLite，並以瀏覽器查看即時數據、歷史資料與圖表。

**建議新機：Raspberry Pi 3B + Raspberry Pi OS Lite 64-bit（Debian 13 Trixie 或 12 Bookworm）。**
不需要 HOBOware、Java、Docker 或 Python pip 套件即可執行服務。Excel 功能使用隨附的 xlrd / xlwt。

## 最短安裝流程

本倉庫為私人倉庫，下載時需要你的 GitHub 權限。已登入 GitHub 的電腦也可從 **Code → Download ZIP** 下載，再傳到 Pi；完整步驟見 [全新 Pi 安裝指南](docs/INSTALL.zh-TW.md)。

在 **新 Pi** 的終端機執行：

```bash
sudo apt-get update
sudo apt-get install -y git gh
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
git clone https://github.com/Max042004/hobo-u30.git
cd hobo-u30
sudo bash install.sh
```

安裝完成後查看本機產生的連線資訊及首次密碼：

```bash
sudo cat /etc/u30-network/connection.txt
python3 /opt/hobo-u30/tools/doctor.py
```

將 U30 USB 接到 Pi，感測器接在 U30 上。安裝器會以目前連接的 U30 序號命名主機；若安裝時未接 U30，則使用此 Pi 的唯一名稱。**以安裝結束顯示的網址為準**，例如 `http://u30-你的序號.local:8080/`。

熱點頁面固定為 **http://10.42.0.1:8080/**。Wi-Fi 名稱與隨機密碼見上述連線卡。
若 `.local` 在手機上無法解析，可先連 Pi 熱點，在連線設定頁查看內網 IP。

## 安裝器會完成的工作

- 程式安裝於 `/opt/hobo-u30`，資料保存在 `/var/lib/hobo-u30`。
- 建立無法互動登入的 `hobo-u30` 服務帳號；不依賴 SSH 帳號或家目錄。
- 設定專屬 USB HID 權限、停用此 U30 的 USB 自動休眠、斷線後依採集間隔重試。
- 安裝開機自動啟動的採集／網頁服務、Wi-Fi 設定服務、mDNS 與熱點 DHCP。
- 為每台 Pi 產生獨立設定密碼，不內建通用密碼或客戶 Tunnel token。
- 預設每運行 7 天自動重啟，開機恢復服務。可用 `--no-weekly-reboot` 停用。
- 設定系統時區為 `Asia/Taipei` 並啟用 NTP。

**初次安裝不會停止、清除或重新部署 U30。** 新資料庫的「自動納入新感測器」預設關閉；需要時在網頁輸入 Pi 設定密碼後自行啟用。這避免安裝新 Pi 時意外清除 U30 既有部署。
Pi 會在讀到有效部署後採用 U30 現有記錄間隔；裝置未連接前仍可開啟網頁。

## 現有功能

- Station Overview：USB 連線、電量、感測通道與目前讀值。
- 資料分析：資料查詢與管理為預設頁；條件篩選、時間區間、可復原刪除。
- 圖表：多序列、時間比較、統計、樣式、限制線、註記；圖表可移除並復原最近刪除。
- CSV / TXT / XLS 匯入匯出，時間軸與 CSV 使用 GMT+8；可儲存與開啟圖表專案。
- 全站採集間隔 1～64800 秒；U30 原生記錄、Pi 查詢與原始存檔使用同一設定。
- 暫停 U30 本機記錄時 Pi 繼續採集；恢復需先備份再重新部署。
- 選用的新感測器自動部署：先暫停、接入，再連續兩次核對；完整備份通過後才清除舊部署並重新開始。
- 網頁修改 Pi 設定密碼，同步修改 Pi 熱點 Wi-Fi 密碼；需要目前密碼與再次確認。
- 完整 512 KB 原始記憶體備份；指標異常時以兩遍全記憶體逐位元組核對保留資料。

此專案提供上述功能，尚未完整復刻 HOBOware；沒有啟用警報／繼電器控制或多台 U30 管理。

## 必須了解的限制

- 完整記憶體備份期間會暫停 Pi 即時讀值數分鐘，完成後恢復。服務重啟及斷線期間也可能漏樣，不會補造資料。
- U30 已循環覆寫（`wrap_count > 0`）的歷史解碼尚未驗證，保留原始 BIN，不聲稱已還原成完整歷史。
- U30 記錄中會忽略新增感測器；先在裝置管理暫停，再接入新感測器。單純移除／替換原有感測器不自動清除部署。
- 暫停後無法原地接續同一部署；恢復／改頻率會先備份再清除原部署。Pi 舊資料與備份保留。
- `CO2` 可能只是電流輸入介面的標籤。顯示 mA 時，需感測器型號、供電與換算規格才能轉成 ppm。
- 目前的原生部署格式提供全機記錄間隔；沒有實作逐感測器硬體取樣／省電控制。
- 熱點不提供網際網路。手機提示無網路時請選保持連線；若無法開啟熱點頁，暫停手機 VPN／行動數據後重試。
- 修改網頁設定密碼不會更新初始交付卡；以新密碼為準。忘記密碼可由 Pi 管理者執行 `sudo python3 /opt/hobo-u30/tools/reset_password.py`。

## 外網使用（選用）

內網可直接使用，外網可選 Cloudflare Tunnel，**不必自行架 VPN**。每台新 Pi 建議獨立 Tunnel／網址，並先配置 Cloudflare Access Email 驗證。

```bash
sudo python3 /opt/hobo-u30/tools/setup_cloudflare.py
```

程式會隱藏輸入 connector token；不把 token 放在 shell 命令列或程式碼。
詳細操作見 [Cloudflare 設定](docs/CLOUDFLARE.zh-TW.md)。不要把兩台同時運行的獨立資料站共用同一個 Tunnel token，以免網址的請求分流到不同資料庫。

## 維護、更新及測試

```bash
systemctl status hobo-u30-server u30-network --no-pager
journalctl -u hobo-u30-server -n 60 --no-pager
python3 /opt/hobo-u30/tools/doctor.py --require-device
```

更新：在原始下載目錄 `git pull --ff-only`，再執行 `sudo bash install.sh`。
現有資料、密碼、Wi-Fi 與自動部署選項保留；服務短暫重啟。先前的程式目錄保留為 `/opt/hobo-u30.previous-*` 供查閱。
舊版位於特定家目錄的手工部署會被安裝器拒絕覆寫，應先在新 Pi 驗證。

測試只需要 pytest；以下不接觸實機 HID：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

不隨倉庫發佈現場完整記憶體映像，因此依賴該私人映像的兩項測試會顯示 skipped；協定小型樣本、資料管理、設定與安裝測試仍可執行。

實機首次安裝與驗收步驟：[安裝指南](docs/INSTALL.zh-TW.md)、[驗收清單](docs/ACCEPTANCE.zh-TW.md)。
原有 Pi 3B+／Debian 12 的 U30 通訊與服務已實測；全新 Pi 3B 的實際安裝由使用者依驗收清單確認，請勿把單元測試視為已完成新硬體實測。

倉庫包含原始碼、必要設定、第三方套件與測試用小型協定樣本；不包含現場資料庫、完整原始備份、客戶密碼、Tunnel token 或 HOBOware 二進位／反編譯原始碼。
第三方授權見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
