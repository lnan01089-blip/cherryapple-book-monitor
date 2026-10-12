# 📚 cherryapple 书店上新监控（云端版）

运行在 **GitHub Actions** 上的 24 小时书店上新监控：每分钟检查一次 cherryapple.myallvalue.com，
发现新书上架立即通过 **Server酱** 推送到微信，附书名、价格、上架时间、库存和购买链接。

**无需电脑开机**——只要微信有网就能收到推送。

## 运行机制

- GitHub Actions 每 4 小时启动一个「班次」（共 6 班/天，覆盖 24 小时且有重叠）
- 每个班次内循环运行 270 轮，每轮间隔 60 秒 → 相当于**每分钟检查一次**
- 每轮抓取店铺全部在售商品，与仓库里的 `state.json` 快照对比：
  - 新商品 ID 出现（上新/补货）→ 微信推送 + 附购买链接
  - 商品售罄/下架 → 微信提示
  - 连续抓取失败约 10 分钟 → 「监控异常」告警
- 快照自动提交回仓库，跨班次持续对比

## 配置

- **微信推送密钥**：存放在仓库 Settings → Secrets and variables → Actions 的
  `BOOK_MONITOR_SENDKEY` 中（不在代码里，公开仓库也不会泄露）
- **忽略关键词**：编辑 `monitor.py` 顶部 `DEFAULT_CONFIG` 里的 `ignore_keywords`
  （默认忽略：邮费/运费/补差价/定制/专用链接/定金/尾款）
- **检查频率**：改 `.github/workflows/monitor.yml` 里的 `--loop 270`（轮数）和 cron 班次

## 手动触发

仓库 Actions 页面 → 「书店上新监控」→ Run workflow，可随时手动跑一班验证。
