# gate-reader-client

独立 Gate reader 的 Python 标准库只读客户端（Python 3.9+，无第三方依赖）。
唯一服务域名：`gate-reader.gatelens.site`；固定 HTTPS、仅 GET、拒绝所有重定向。
这是 REST 服务，不是 MCP，也没有下单、撤单或修改账户的接口。

## 安全配置

在云环境 Network secret 中配置服务所需的 `READER_TOKEN`，允许域名严格设为
`gate-reader.gatelens.site`。进程环境变量 `READER_TOKEN` **只引用该环境提供的代理占位符**，
真实口令仅由平台代理替换。沿用平台的 `HTTPS_PROXY` / `https_proxy` 配置，不绕过代理。
此客户端不会创建凭据、改权限、读取凭据文件或显示口令。缺少变量时，在发起受保护请求前失败。
不要在命令行参数、README、`.env`、测试、日志或提交中填写真实口令或账户标识。
占位符的具体格式由当前云环境提供，客户端不猜测或生成占位符。

当前仓库公开。账户结果只应在获授权的私有环境查看；不要重定向输出到此仓库、保存真实响应，
或粘贴到 issue / 提交。错误仅显示安全的固定摘要或 HTTP 状态码，不显示上游错误正文。

## 使用

在仓库目录运行：

```sh
python -m unittest discover -s tests -v
python -m gate_reader health
# 下面的受保护操作：先配置 Network secret，并获得真实账户验证授权
python -m gate_reader positions --contract MSFT_USDT
python -m gate_reader orders --limit 100 --offset 0
python -m gate_reader position-history --contract MSFT_USDT --from 1700000000 --to 1700086400
python -m gate_reader trades --history --from 1700000000 --to 1700800000
```

示例合约和时间仅说明参数格式，不表示实际账户持仓。
普通查询输出服务原始 JSON envelope；`--history` 逐行输出带读取上下文的历史记录：
`record` 保留原始条目，`reader_context` 包含窗口、页 offset、页内 index 和
`possible_duplicate`。后者仅标记此前出现过相同 JSON 内容，不表示已证实为同一事件。
所有条目均保留，不能直接把重复标记作为删除依据。
`--timeout` 默认 20 秒（urllib 的网络操作超时，并非整次历史读取的总期限）。

```python
from gate_reader import Client

client = Client(timeout=20)
# 授权后按需读取一个资源；不会自动读取其他资源
response = client.query("positions", contract="MSFT_USDT")
# 分页保留完整 envelope；仅显式 pagination.next_offset == null 才终止
for page in client.pages("orders", limit=100):
    pass  # 在私有内存中处理，不持久化账户数据
# 历史接口默认 data 是数组；若服务返回嵌套结构，需依据实际 schema 显式传 records_path
for record in client.history("trades", 1700000000, 1700800000):
    pass
# 如需标记内容相同的潜在重复，并追溯请求窗口/页，传 include_context=True
# 每次仍返回全部条目，原始条目位于结果的 record 字段
```

## 接口与历史边界

- `/health` 公开，只验证 reader 存活，不能证明 Gate 或鉴权可用。
- `/account`、`/positions`、`/orders`、`/order-history`、`/trades`、`/closed-positions`、
  `/position-history`、`/account-book`、`/trigger-orders`、`/snapshot` 需要 Bearer。
- 分页参数为 `limit`（1–100）和 `offset`（非负整数）；`positions` 默认 `holding=true`。
- 历史接口为 `order-history`、`trades`、`closed-positions`、`position-history`、`account-book`。
  `from` / `to` 为 Unix 秒，必须成对提供；省略时固定最近 24h。
  单次查询窗口最长 7 天；`position-history` 必须传 `contract`。
- `history()` 分窗读取每个窗口的全部页：例如 `[0, 604800]` 后请求
  `[604800, 604801]`，两次请求共享边界秒；这里方括号仅描述传入参数，
  不声称服务器的端点包含规则已得到确认。若两侧都包含边界，可能重复；若两侧
  都排除边界，仍可能遗漏边界事件。单点范围只请求一次，不保证服务器会返回该秒事件。
  不推断 ID、时间戳或缺失字段，不按相同内容丢弃条目：合法的不同成交或资金流水
  可以有完全相同的 JSON。默认返回全部原始条目；`include_context=True` 仅标记
  内容相同的潜在重复，并保留请求来源。此模式的标记集合占用随历史增长的内存。
  尚无经文档确认的稳定唯一 ID，因此没有启用任何事件去重。
  此结果不是完整账本，也不保证实时变化或未知边界规则下的历史完整性；见到缺失
  分页字段或非递增 offset 会报错，不假设空页意味着结束。
- envelope 包含 `ok/source/endpoint/query/requested_at/received_at/data`；客户端保留原始字段。
  仅要求 `ok` 为 true，不补造其他字段。上游失败可返回 502 和结构化错误；客户端不输出正文。
- `snapshot` 是八次独立读取，只含最近 24h 的第一页，不能作为完整历史或原子快照；
  客户端不对它自动分页，也不默认调用它。

测试全部采用模拟数据和传输，不需要密钥，也不会联网。真实账户验证必须等待父线程确认安全配置完成。
