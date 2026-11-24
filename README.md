# 耳标牲畜跨栏转群与复核采录台

拍卖场记录员在一台离线电脑上接收 RFID 门扫、手持复核和地磅打印票。场务按买家和代养场约定逐头分栏，交易员用点数和秤盘清单封账。交割表逐头记录卖方、买方、代养方、目标栏和取证凭据。数据、人物、时间和设备均为合成。单库代表一个拍卖班次，牲畜当班只可成交一次。

## 运行

Python 3.11+，项目根目录直接运行，只使用 Python 标准库及其 `sqlite3`。

```powershell
python -B -c "import sqlite3, ear_tag_transfer; print(ear_tag_transfer.__file__, sqlite3.sqlite_version)"
python -B -m ear_tag_transfer demo
python -B -m unittest discover -s tests -v
```

以下命令演示落盘、迟到凭据与恢复。`shift.sqlite` 建议使用此前未创建的文件名。

```powershell
python -B -m ear_tag_transfer init --db shift.sqlite
python -B -m ear_tag_transfer ingest --db shift.sqlite --input fixtures/auction_shift.json
python -B -m ear_tag_transfer ingest --db shift.sqlite --input fixtures/late_bridge.json
python -B -m ear_tag_transfer report --db shift.sqlite --evidence
python -B -m ear_tag_transfer ingest --db shift.sqlite --input fixtures/late_resolutions.json
python -B -m ear_tag_transfer replay --db shift.sqlite
python -B -m ear_tag_transfer replay --db shift.sqlite --repair
```

主样例录入 39 条凭据。`LOT-17` 两头，重验前 764,000 克，最终 762,750 克。买方 Maple 取得 A01、A02 的所有权，Juniper Yard 和 Willow Yard 分别代养，P-STAGE 清场，A03 仍由卖方待售。补传样例使 P-STAGE 显示 `clear_disputed`，已成交批出现秤票争议。核销后两者恢复，交割表的原重量、产权凭据和清场签证始终保留。

## 四个工作台

`identity`、`yard`、`weigh`、`sale` 各接受 `--db` 和 `--input`，每个入口只处理自己的事件。`ingest` 可按收件顺序录入混合文件。每文件为非空 JSON 数组，每项至少有唯一 `id`、`type`、UTC 秒精度 `at`。样例中 `OUT` 表示场外，不能作为真实栏号。变量字段见下表与 `fixtures/auction_shift.json`。`expected` 是经办员在屏幕上看到的直接前版，首版填 `null`。

| 工作台 | `type` | 操作字段 |
|---|---|---|
| 身份 | `register-animal` | `animal`, `tag`, `mark`, `owner` |
| 身份 | `retag` | `animal`, `prior_tag`, `new_tag`, `reason` |
| 身份 | `review-scan` | `scan`, `expected`, `outcome`, `operator`, `reason`。`confirm` 另填 `mark`，`exclude` 表示排除该扫。 |
| 围栏 | `open-pen` | `pen`, `keeper` |
| 围栏 | `pen-contract` | `pen`, `keeper`, `owner`, `action` (`grant` / `revoke`), `expected` |
| 围栏 | `gate-scan` | `from`, `to`, `tag`, `reader` |
| 围栏 | `move` | `animal`, `scan`, `from`, `to`, `owner` |
| 围栏 | `drain-pen` | `pen`, `keeper` |
| 围栏 | `clear-pen` | `pen`, `keeper`, `walked_by` |
| 秤台 | `open-load` | `load`, `deck`, `animals`（逐头编号数组） |
| 秤台 | `scale-ticket` | `load`, `receipt`, `gross_g`, `tare_g`（整数克） |
| 秤台 | `choose-ticket` | `load`, `ticket`, `considered`（全部待裁票号数组）, `expected`, `reason` |
| 成交 | `offer-lot` | `lot`, `seller`, `buyer`, `expected_count`, `members`（`animal` 和 `to` 的对象数组） |
| 成交 | `cancel-lot` | `lot`, `seller`, `reason` |
| 成交 | `seal-lot` | `lot`, `expected`, `scans`, `loads` |
| 成交 | `close-lot` | `lot`, `seller`, `buyer`, `tally` |

### 现场规则

- RFID 原始读数以设备发生时刻查耳标历史有效区间。已发耳标不再转配。手持确认必须给出有效可见记号，已知耳标和手持记号必须指向同一个体。原始扫描保留，排除和重认追加独立复核版。
- 代养方按买家签收约定。普通移栏按当前所有权检查目标栏接收约定，使用复核过的同一只、同方向门扫，从当前栏位移出并一次性销号。封栏只准出。清场还需另一人走查、零在栏、已对清截至签证时的既有门扫，以及无争议的已记移栏；清场后收到的异常显示在实时状态。
- 同桥打印号只认一次。待裁票须全列，秤员可保留原票或采用本轮待裁中的一张票。即使有旧选票，迟到的未裁票也会暂停秤盘可用性。
- 成交台逐头预占同一卖方有权且当班未售的牲畜，关联买家在目标栏的接收约定。`report` 中待交割批的 `allocations` 给出每头当前来源、目标和阻断原因，`release` 给出封账是否还可执行。撤销释放未交割预占。
- 封账逐头匹配预约、门扫、目标栏及秤盘成员。各秤盘互不重叠且只含同一目标栏的动物。所选票须发生于该动物当前入栏执行至目标门扫之间。封账保存复核、当前栏位、代养约定及所选票的版本和分栏重量。交割重新核验所有证据，事务内同时转移产权、记目标栏和代养方、消耗门扫并清理预占。
- 成交后新证据可产生异常，原始交割继续存档。手持重新确认同一只可解除身份争议，选回冻结票可解除其重量争议。真实但未计入的迟到门扫保持可见，清场仍显示争议。

### 失败、重试与重算

控制动作必须按严格递增的 UTC 秒提交。离线门扫和秤票允许迟到，收件顺序重算当时裁决，发生时间用于检查耳标区间和现场顺序。相同事件 ID 和相同内容重试幂等，内容变化时整个输入文件回滚；同一事务内写入收件日志与所有业务投影。业务拒绝退出码为 `2`。`fixtures/reject_*.json` 可用于重现错记身份、占栏清场、同桥重票和同头数错配，测试注明各自所需的有效前缀。

`replay` 验证本地收件编号和摘要链，在空库按原顺序重算所有领域表，再对照当前投影。发现差异时以 `1` 退出，`--repair` 在同一写事务中替换投影；原始收件日志保留。损坏的日志拒绝修复。本地摘要可检查存储损坏，设备及经办人身份由输入现场提供。

模块 `identity` 管耳标和复核，`yard` 管代养约定、栏位和签证，`weighing` 管实体秤盘和裁票，`sale` 管预占、封账和产权交割。`report` 汇合证据，`ledger` 和 `schema` 管事务、SQLite 与恢复。
