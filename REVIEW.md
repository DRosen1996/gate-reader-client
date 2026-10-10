# 本地可靠账本与单笔回放

新增独立 `review` 服务，不改变既有 gate_reader 的固定 HTTPS、GET 和代理认证。默认仅监听回环地址；没有交易写接口、部署动作或持续采集任务。

## 事实与核算

SQLite 是唯一事实主账本。原始 JSON envelope、上下文、摘要和观察时间追加保存；稳定来源 ID 用于幂等记账。成交使用合约和 trade_id（显式 id 兼容），流水与订单使用 id。返回的来源条目保持原样，不用内部键覆盖原始字段。缺失身份、无效金额及成交/流水身份冲突被隔离，保留证据并显示缺口。订单保留每次观察，包括 A→B→A。

每页证据、事件与断点同一事务提交。中断从最近提交的 next_offset 恢复；七天窗口共享边界秒。完成窗口的连续覆盖决定数据截止，中间缺页不能被较晚完整窗口掩盖。本地观察时间与历史截止分开。重复同范围 sync 复用完成窗口，扩大截止重读末窗口；重复导入增加观察证据，不重复记账。

金额为 Decimal 和文本，拒绝浮点与非有限金额。计算精度按来源金额的整数/小数跨度扩展，资金分配最后一项吸收余数。数字成交 ID 按数字排序；周期键由开仓来源身份生成，补导其他交易不改变笔记归属。支持部分平仓与反向成交。首次成交显示已有平仓量时，缺少期初仓位锚点，该合约周期推导停止；双向模式也停止推导。

资金流水是现金核算权威，成交手续费只作证据，不再次扣减。展示分别包括：已实现成交盈亏 pnl、手续费 fee、资金费 fund、出入金 dnw、未知类别 other。期间交易净现金流为 pnl+fee+fund，包含未闭仓周期已发生的费用；不等同于闭仓周期合计。出入金与未实现快照独立展示。流水按成交 ID 与合约归属；资金费按持仓时间归属，多个候选时明确未分配。

余额桥接以首尾余额观测推导期初及核对期末，计入两者之间所有流水，包括没有中间余额字段的流水。缺少时间则未知，锚点之外仍有流水则标部分覆盖，残差非零则标缺口。零残差只说明已读数据的数值桥接，不证明完整历史。所有指标明确为“暂计”：上游边界规则尚未确认，缺页、冲突与未分配原因可展开。无独立行情源，不生成 K 线或 MAE/MFE。

计划、决策、复盘均追加版本；历史交易编辑始终标为事后补记。缺失历史事前计划显示未知。本版没有下一笔尚未成交交易的计划流程。schema v1 笔记通过追加别名迁移到稳定周期键，不改写旧笔记；无法确认归属的笔记单独展示。

## 本地服务与安全

私有目录必须在仓库之外、归当前用户所有、权限 0700，文件仅归用户可读写。已有宽权限路径直接拒绝，不自动修改权限。真实数据、数据库、备份与令牌不得进入 Git、PR、Library、公共页面或截图。以下变量和路径都是占位示例，需由操作者设置；没有真实环境地址或账户信息。

```sh
DATA_DIR=/path/to/private-data
python -m review --db "$DATA_DIR/ledger.sqlite" sync --start "$START_UNIX_SECONDS" --end "$END_UNIX_SECONDS"
python -m review --db "$DATA_DIR/ledger.sqlite" check
python -m review --db "$DATA_DIR/ledger.sqlite" serve --port 8765
```

sync 按需只读成交、流水、历史订单、账户、仓位、当前订单和 snapshot；snapshot 为非原子快照且历史只有第一页，不能代替同步。沿用已有 READER_TOKEN 网络代理配置，不创建或更改凭据。

HTTP 严格核对 Host，拒绝重复 Host；写入 Origin 必须与该 Host 精确同源，拒绝重复、缺失、null 或其他 Origin。无 CORS，拒绝跨站 Fetch Metadata；写入仅接收有界 JSON、单一长度头，不接收 Transfer-Encoding。界面来源及笔记均用 textContent，安全策略禁止第三方脚本、嵌套框架和 base 注入。本地服务不是多用户认证系统，不能直接开放公网。

## 备份、恢复与迁移

使用 SQLite DELETE 日志、FULL 同步和外键，未启用 WAL。备份覆盖整个数据库：原始证据、观察版本、分页范围、缺口、笔记与周期别名。

```sh
python -m review --db "$DATA_DIR/ledger.sqlite" backup "$DATA_DIR/backup.sqlite"
python -m review --db "$DATA_DIR/restored.sqlite" restore "$DATA_DIR/backup.sqlite" --sha256 "$VERIFIED_SHA256"
python -m review --db "$DATA_DIR/restored.sqlite" check
```

backup 用 SQLite backup API 写私有临时文件，检查完整性、外键、证据摘要及 schema，同步文件和目录后独占原子发布，禁止覆盖。restore 校验同一份捕获字节的 SHA256，再在临时文件检查数据库并原子发布；不重新打开变动来源进行内容校验。恢复新路径后比较重建 view，停服务再切换路径，保留旧文件。支持 v1 到 v2 的笔记别名迁移；未知 schema 拒绝打开。备份仍属私有账户数据，本版不创建加密凭据、自动备份或采集任务。

## 验证与前端维护

```sh
python -m unittest discover -s tests -v
python -m compileall -q review tools
node --check review/static/app.js
python tools/verify_browser.py
```

测试全部采用合成数据；浏览器脚本需要开发环境已有 Playwright 和 Chromium，不是生产依赖。可用 --screenshot 保存合成截图。GitHub Actions 仅运行合成测试和静态检查，不连接 reader、不使用账户密钥、不上传真实数据。远端 CI 是否通过必须核对具体 commit，不能用本地测试替代。

选择标准库 HTTP 与原生 JavaScript 是为了适配原仓库的零运行依赖客户端，并把第一版范围集中在可靠账本。前端有独立 HTML/CSS/JS，业务核算留在 Python，文本展示集中于安全 DOM 辅助函数，已有浏览器回归。没有 TypeScript 类型检查或前端打包步骤，node --check 只检查语法；如果页面复杂度增加，可在保持 API 与验收用例的前提下迁移 React/TypeScript。

## 上线步骤（尚未执行）

1. 获得独立部署授权及目标服务器 SSH/运维访问，确认系统用户、Python、SQLite 和持久磁盘目录。
2. 经私有通道迁移数据库备份，校验摘要、权限、完整性及重建结果；代码发布不携带数据库。
3. 确认目标服务器已有 reader 认证与代理接入，不能直接复制开发环境的代理占位符作为凭据。
4. 用独立低权限用户运行回环服务；远程查看优先已有 SSH 隧道。反向代理、TLS、访问认证及开放端口另需明确授权。
5. 确认运维备份保留和离机恢复演练，再获授权配置服务管理。本阶段不合并、不部署、不改变权限或新增持续任务。
