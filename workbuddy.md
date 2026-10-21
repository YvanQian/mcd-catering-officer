# WorkBuddy 开发记录

本文件记录本项目在 **WorkBuddy** 中的完整开发过程，用于核验 WorkBuddy 联动活动条件。

- 开发时间：2026-10-21 19:04 — 19:45（北京时间）
- 开发工具：WorkBuddy
- 使用的 MCP：麦当劳中国 MCP Server（`mcd-mcp`，Streamable HTTP）
- 产物：可运行的团餐决策引擎（Python，零第三方依赖）+ 5 份参赛文档

---

## 对话脉络

### 1. 了解赛事规则

**输入**：赛事仓库链接 <https://github.com/M-China/mcd-developer-innovation-challenge> + "看下这个怎么参加"

**WorkBuddy 动作**：抓取 README、`activityGuidelines.md`、`RANKING.md`，梳理出关键约束：

- 报名截止 2026-10-25 23:59，10-26 00:00 定榜
- 必备文件：README / CONTEST_DECLARATION / MCP_INTEGRATION / mcp-config.example.json / workbuddy.md
- 报名 Issue 有三个格式陷阱：标题正文不换行、字段内容不换行、项目地址不用 Markdown 链接
- 发现官方 `activityGuidelines.md` 要求 `mcp-config.example.json`，但 README 表格漏列了这一项

### 2. 获取 Token 并配置连接

**输入**："怎么获取mcp token"

**WorkBuddy 动作**：定位到真正的入口是 <https://open.mcd.cn/mcp>（而非 API 端点 `mcp.mcd.cn`，后者浏览器直连会返回 403 系统错误），给出五步流程。

**输入**：Token 字符串

**WorkBuddy 动作**：

1. 先用 `initialize` 握手验证 Token 有效性（HTTP 200，`serverInfo: mcd-mcp v1.0.0`）
2. `tools/list` 拉出全部 **35 个 Tool**
3. 真实调用 `now-time-info` 验证端到端可用
4. 合并写入 `~/.workbuddy/mcp.json`（保留原有 `magpie-image`、`lighthouse-ops`，不覆盖）

### 3. 选题判断

**输入**："推荐几个方向"

**WorkBuddy 动作**：先做数据取证，再给建议——

1. 拉取 35 个 Tool 的完整 `inputSchema`，看每个工具的真实参数契约
2. 统计榜单 100 个项目的赛道分布：省钱/优惠券 20 个、点餐助手 15 个、娱乐玄学 12 个、梗周边 10 个、热量健身 8 个、派对活动 8 个、积分抽奖 2 个、**团餐 0 个**
3. 实测验证冷门赛道是否可行：
   - `delivery-query-stores(beType=6)` 成功返回 3 家团餐门店
   - `query-meals(beType=6)` 返回完整团餐菜单
   - `query-meal-assistance` 返回 3 种助餐服务
4. 给出三个方向并按可行性排序，明确不推荐积分/省钱/热量赛道

**关键判断依据**：`query-nearby-stores` 的 `beType` 枚举只有 1 和 5，查不到团餐门店——这个技术门槛正是团餐赛道空白的原因，也是差异化壁垒。

### 4. 正式开发

**输入**："做方向一"

**WorkBuddy 动作**：先摸接口再写代码，避免写出跑不通的演示。

验证过程中发现四个关键事实，直接决定了产品定位：

| 发现 | 影响 |
|---|---|
| 同一单换助餐服务，实付差 108~160 元 | 从"配餐助手"升级为"比价决策工具"，成为核心卖点 |
| `reservationDate` 会让 `GMS002/GMS003` 从 `enable:false` 变 `true` | 提前 4 小时规则可做显式提示 |
| `canWithOrder` 在团餐场景恒为 `false`，但算价正常 | 改用 `calculate-price` 作为可下单性校验 |
| 所有金额单位为分 | 统一除以 100 展示，避免出现 ¥75800 |

---

## WorkBuddy 在本项目中的实际作用

| 环节 | 人工/AI 分工 |
|---|---|
| 赛事规则梳理 | WorkBuddy 抓取三个文件并交叉比对，发现文档间的字段矛盾 |
| Token 配置 | WorkBuddy 直接验证并写入 MCP 配置，未手工拼 JSON |
| 选题决策 | WorkBuddy 拉取榜单做赛道计数，并实测验证冷门赛道的可行性 |
| 接口契约 | WorkBuddy 导出 35 个 Tool 的 schema，逐个核对必填参数与枚举 |
| 代码实现 | WorkBuddy 编写 `mcd_client.py` / `catering.py` / `demo.py` |
| 调试验证 | WorkBuddy 真实调用 API 跑通全链路，修复 `--service` 参数缺失、预算利用率过低两个问题 |
| 文档撰写 | WorkBuddy 输出 5 份参赛文档 |

## 复现方式

1. 在 WorkBuddy 中配置 `mcd-mcp` 自定义连接器（见 `mcp-config.example.json`）
2. 在对话中说出需求，例如：*"明天中午 20 人，人均 40，帮我看看团餐怎么订最划算"*
3. WorkBuddy 会按 `SKILL.md` 的流程调用 `catering.py`，输出配餐方案与助餐服务比价

## 备注

- 本项目全部代码在 WorkBuddy 会话中直接生成并当场验证，未使用其他 IDE
- 所有价格数据均为 2026-10-21 当日真实返回，未做任何模拟或编造
- 真实下单始终为 dry-run，开发全程未产生任何真实订单
