# 全新 Raspberry Pi 3B 安裝流程

## 1. 準備系統

若已完成刷機與 SSH，可直接到第 2 步。

1. 使用 [Raspberry Pi Imager](https://www.raspberrypi.com/software/)，裝置選 Raspberry Pi 3，系統選 **Raspberry Pi OS Lite 64-bit**。官方列出 3B 支援 64-bit；支援本套件的版本是 Debian 12 Bookworm／13 Trixie。
2. 在 Imager 自訂設定建立自己的登入帳號與密碼，啟用 SSH，設定時區及 Wi-Fi 國家。需要無線初始安裝時填入 **2.4 GHz Wi-Fi**；原始 3B 的板載 Wi-Fi 不支援 5 GHz。
3. 寫入 microSD 後開機。第一次建議插網路線，避免設定 Wi-Fi 時找不到 Pi。需有網際網路下載 Debian 套件。
4. 電腦以 Imager 設定的帳號連入；這不是網頁 Pi 設定密碼：

```bash
ssh 你的帳號@樹莓派IP
cat /etc/os-release
uname -m
```

64-bit 應顯示 `aarch64`。Python 程式可在 32-bit ARM 執行，但本次新機優先驗證 64-bit，避免混用不同映像。
電源與 USB 線要穩定，U30 電池／感測器供電仍須正常。

## 2A. 直接從私人 repo 下載（推薦）

這個 repo 是 private，所以新 Pi 需要有讀取權限的 GitHub 身分。以下使用 GitHub CLI 的裝置登入；不必把 PAT 或帳號密碼貼到指令中。

```bash
sudo apt-get update
sudo apt-get install -y git gh
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
git clone https://github.com/Max042004/hobo-u30.git
cd hobo-u30
sudo bash install.sh
```

`gh auth login` 會提示驗證網址與一次性代碼。用電腦／手機瀏覽器自行登入有 repo 權限的帳號完成。
已設定 GitHub SSH 金鑰者也可用 `git clone git@github.com:Max042004/hobo-u30.git`。

## 2B. 從電腦下載 ZIP 再傳到 Pi

1. 電腦瀏覽器登入 GitHub，開啟 repo，選 **Code → Download ZIP**。
2. Windows PowerShell 在下載目錄執行，將目標改成自己的帳號與 IP：

```powershell
$PiTarget = '你的帳號@樹莓派IP'
scp .\hobo-u30-main.zip "${PiTarget}:~/"
ssh $PiTarget
```

3. Pi 上解壓安裝：

```bash
sudo apt-get update
sudo apt-get install -y unzip
unzip hobo-u30-main.zip
cd hobo-u30-main
sudo bash install.sh
```

ZIP 內沒有 Git 歷史，日後更新可下載新 ZIP、在新解壓目錄執行安裝器；資料仍留在 `/var/lib/hobo-u30`。

## 3. 檢查首次安裝

安裝時可接上 U30，藉此使用其序號命名；未接時會採用 Pi 自己的唯一名稱，不影響日後接上 U30。

```bash
sudo cat /etc/u30-network/connection.txt
python3 /opt/hobo-u30/tools/doctor.py
systemctl status hobo-u30-server u30-network --no-pager
```

應看到網頁服務與網路服務 `active`。`doctor.py` 不會開 USB 會話、不會重新部署 U30，也不顯示密碼或 Tunnel token。
沒有接 U30 時的 `usb_connected=false` 可以是正常結果；網頁應照常開啟。

## 4. 開啟網頁

### 網路線

手機／電腦與 Pi 在同一內網，開啟安裝輸出的 `http://主機名稱.local:8080/`。
IP 仍由路由器 DHCP 分配，也可用 `hostname -I` 找到 IP，開啟 `http://IP:8080/`。

### Pi 熱點

插網路線時會開啟設定熱點。手機連接連線卡上的 SSID，輸入該卡的隨機密碼。
若手機顯示沒有網際網路，選擇保持連線，再開啟 **http://10.42.0.1:8080/**。
熱點不提供上網；VPN／行動數據可能使手機將請求走到別的網路。

### 家用 Wi-Fi

在網頁「連線設定」展開 Wi-Fi 設定，輸入家用 SSID、密碼與 **Pi 設定密碼**。
成功後手機切回家用網路，使用內網網址。單一 Wi-Fi 無線電無法同時維持家用連線與設定熱點；失敗會回復熱點。
只有 Wi-Fi 開機時，服務會先使用 Imager／先前保存的 Wi-Fi；連不上才提供熱點。
支援一般 WPA2／WPA2-WPA3 混合模式，未提供企業認證或需瀏覽器登入的 Wi-Fi 設定。

## 5. 接上感測器並開始使用

1. 感測器接在 U30，U30 USB 接 Pi；等待首次讀取。
2. 首頁應顯示 U30 序號、電量與目前通道。
3. 「資料分析 → 資料查詢與管理」選 **樹莓派即時採集**，查看已保存讀值。
4. 若 U30 已有有效的部署，Pi 使用該記錄間隔；原生記錄沒有開始時，Pi 仍可讀取及保存。
5. 「裝置管理」可以設定頻率、暫停／恢復與下載備份。更改原生部署需要密碼及清除確認，先下載並核對完整備份。
6. 新機自動納入感測器預設關閉。要使用時，勾選並輸入密碼儲存；之後先暫停 U30 再新增感測器，兩次辨識一致後會自動備份與重新部署。

網頁變更設定密碼會同步改變 Pi 熱點密碼。原始連線卡不自動改寫，請自行保管新密碼。忘記時可在 SSH 執行：

```bash
sudo python3 /opt/hobo-u30/tools/reset_password.py
```

程式隱藏輸入，且不變更 SSH 帳號密碼、家用 Wi-Fi 或 Cloudflare Email。

## 6. 選用設定與操作

```bash
# 先檢查安裝環境與預計路徑，不修改系統
sudo bash install.sh --dry-run

# 首次安裝指定 .local 名稱（之後更新會保留該名稱）
sudo bash install.sh --hostname u30-lab

# 不啟用每 7 天自動重啟
sudo bash install.sh --no-weekly-reboot

# 安裝後關閉／開啟每週重啟
sudo systemctl disable --now u30-weekly-reboot.timer
sudo systemctl enable --now u30-weekly-reboot.timer
systemctl list-timers u30-weekly-reboot.timer --no-pager
```

重啟計時從 timer 啟動開始；每次開機後重新計算 7 天。安裝器不會立即重啟。

## 更新與資料位置

| 項目 | 位置 |
|---|---|
| 執行程式 | `/opt/hobo-u30` |
| 即時／歷史資料庫 | `/var/lib/hobo-u30/station.sqlite3` |
| 原始記憶體備份及部署稽核 | `/var/lib/hobo-u30/backups` |
| 熱點／設定密碼 | `/etc/u30-network/config.json`，root 專用 |
| 首次連線卡 | `/etc/u30-network/connection.txt`，root 專用 |
| 選用 Tunnel token | `/etc/cloudflared/tunnel-token`，root 專用 |

在原始下載目錄更新後重跑 `sudo bash install.sh`。安裝器不清除上述資料，保留已變更的密碼／Wi-Fi／選項。
更新會短暫重啟服務，U30 原生記錄繼續；Pi 即時讀值可能有短暫空檔。
舊程式保留於 `/opt/hobo-u30.previous-*`；資料庫不放在程式目錄內。

## 常見問題

- **clone 顯示找不到 repo／需要登入**：repo 為 private，先完成 GitHub CLI 或 SSH 金鑰登入，或改用 ZIP。
- **服務正常但 USB 未連接**：檢查 USB 線、U30 電池，等待採集間隔或按「重新讀取」。可重插 USB；避免同時執行另一個 HID 診斷程式。
- **偵測不到新插入的感測器**：U30 記錄中會忽略新增感測器，先在網頁暫停再接上。
- **CO₂ 顯示接近 0 mA**：軟體辨識到的是電流轉接器；請核對 CO₂ 本體供電、接線與型號，不能把標籤直接當作 ppm。
- **時鐘未同步**：檢查外網與 `timedatectl status`；3B 無備援 RTC，沒有正確時間時不應重新部署 U30。
- **舊部署安裝被拒絕**：這是保護現有家目錄部署；請在全新 Pi 測試，不要直接刪除既有設定來繞過檢查。
- **安裝失敗**：查看錯誤後重新執行安裝器。資料與憑證保留；`journalctl -u hobo-u30-server -u u30-network -n 80 --no-pager` 可看服務日誌。分享日誌時勿附密碼檔、token 或私人連線卡。

官方參考：[Pi 系統下載與相容機型](https://www.raspberrypi.com/software/operating-systems/)、[初始刷機與設定](https://www.raspberrypi.com/documentation/computers/getting-started.html)。
