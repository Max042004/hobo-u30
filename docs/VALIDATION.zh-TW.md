# 套件驗證紀錄

研究／封裝日期：2026-10-28（Asia/Taipei）。

- 執行中的 U30 軟體已從既有專案整理為獨立安裝包，排除現場資料、客戶憑證與原始 HOBOware 程式。
- Windows Python 3.12 與 Linux Python 3.10 的乾淨原始碼測試通過。
- [GitHub Actions 的 Python 3.11、3.13 測試](https://github.com/Max042004/hobo-u30/actions)均啟用；狀態以目前 commit 的執行結果為準。
- 安裝整合測試使用拋棄式檔案系統與模擬 systemd／NetworkManager，核對首次安裝、資料庫初始化、獨立密碼、重複安裝保留資料與密碼、拒絕覆寫舊部署，以及初次不自動重新部署 U30。
- Shell 與 JavaScript 語法檢查、追蹤檔案的憑證／私人資料掃描通過。
- 兩項需要現場完整 512 KB 映像的歷史測試會略過；不把私人映像上傳來強行滿足測試。

這些是軟體與安裝流程驗證，**尚未替使用者在全新的 Pi 3B 上執行實機安裝**。
首次 apt 安裝、真實 NetworkManager 熱點切換、USB 拔插與重啟恢復，請依 [新機驗收清單](ACCEPTANCE.zh-TW.md)在該 Pi 手動確認。
