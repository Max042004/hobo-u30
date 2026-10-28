# Cloudflare Tunnel 外網設定（選用）

先完成內網安裝與 USB 驗收。這一步需要你自己的 Cloudflare 帳號、DNS 網域、Tunnel connector token 與允許的 Email，倉庫沒有客戶憑證。

## Cloudflare 控制台

1. 建立這台 Pi 專用的 Cloudflared Tunnel。不要把兩台獨立資料站共用同一個 token：多個 connector 可能將同一網址的請求分流至不同 Pi。
2. 先為預定 hostname 建立 **Access → Applications → Self-hosted**，Allow policy 僅列指定 Email；設定 Email One-time PIN。
3. 設定該 hostname 的 Tunnel route：HTTP、**`localhost:8080`**。
4. 在該 route 的 Access／Additional application settings 啟用 **Protect with Access** 並選擇正確 Access application，讓 connector 驗證 Access token。介面名稱可能隨 Cloudflare 版本調整；請依控制台對應設定。
5. 複製 connector 安裝命令中的完整 token。**Tunnel UUID 不是 connector token**；不要把整段安裝命令或帳號密碼輸入下列程式。

## Pi 安裝 connector

```bash
sudo python3 /opt/hobo-u30/tools/setup_cloudflare.py
```

在隱藏輸入提示貼上 token。程式會從 Cloudflare 官方 APT repository 安裝 cloudflared，將 token 存為 root 專用 `/etc/cloudflared/tunnel-token`，設定 systemd 開機啟動。
token 不寫入命令參數、Git、README、日誌或網頁狀態。已存在不同 token 時不自動覆蓋。

若你已將 token 單獨保存成 root 可讀檔案，也可用：

```bash
sudo python3 /opt/hobo-u30/tools/setup_cloudflare.py --token-file /安全位置/connector-token.txt
```

不需要在路由器開 inbound port 或設定 VPN。

## 驗證

```bash
systemctl status cloudflared-u30 --no-pager
journalctl -u cloudflared-u30 -n 40 --no-pager
curl --fail http://127.0.0.1:20241/ready
```

Cloudflare 控制台應顯示 Healthy。手機關閉 Wi-Fi，使用行動網路與未登入的瀏覽器開啟 hostname：

1. 必須先顯示 Access 登入。
2. 輸入允許的 Email，使用收到的驗證碼登入。
3. 正確進入 U30 首頁，確認這是新 Pi 的裝置／資料。
4. 未授權的 Email 不能進站。

Cloudflare Access 保護外網入口；內網網址維持原設計，可讀取資料，敏感設定另要求 Pi 設定密碼。
更改 Pi 設定密碼不會變更 Cloudflare 允許 Email 或登入方式。

若 UDP 出口受限，cloudflared 通常會回退 TCP；請依日誌確認 DNS、時間與對外連線。
停止外網 connector：`sudo systemctl disable --now cloudflared-u30`，內網及採集仍可使用。

官方參考：[cloudflared 安裝套件](https://pkg.cloudflare.com/)、[建立 Tunnel](https://developers.cloudflare.com/tunnel/get-started/)、[執行參數](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)。
