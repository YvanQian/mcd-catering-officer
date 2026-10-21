# MCP 集成说明

本文说明「麦麦团餐官」实际调用的麦当劳 MCP 能力、参数契约、调用时序，以及这些调用如何转化为业务价值。

## 一、所用 MCP Server

| 项 | 值 |
|---|---|
| Server 名称 | 麦当劳中国 MCP Server（`mcd-mcp`） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| 协议版本 | `2025-06-18` |
| Server 版本 | `1.0.0`（实测 `serverInfo`） |
| 可用 Tool 总数 | 35 个，本项目使用其中 6 个 |
| Token 获取 | <https://open.mcd.cn/mcp> → 登录 → 控制台 → 激活 |
| 限流 | 600 次/分钟，超出返回 429 |

## 二、使用的 Tool 及用途

| # | Tool | 用途 | 关键参数 |
|---|---|---|---|
| 1 | `delivery-query-addresses` | 取用户已保存的收货地址，团餐必须绑定配送地址 | 无 |
| 2 | `delivery-query-stores` | **发现团餐门店**，返回门店编码、业务编码、营业状态、未来 7 天档期 | `beType=6`（企业团餐）、`addressId` |
| 3 | `query-meals` | 查团餐菜单，按预约时段返回可售商品与价格 | `storeCode`、`orderType=2`、`beType=6`、`beCode`、`reservationDate` |
| 4 | `query-meal-assistance` | 查助餐服务（保鲜速达 / 专人分餐 / 专人摆台）及可用性 | `storeCode`、`beType=6`、`orderType=2`、`beCode`、`reservationDate` |
| 5 | `calculate-price` | **算价与折扣试算**，返回原价、实付、优惠、促销名、下一档折扣差额 | 同上 + `gmServiceCode`、`items`、`needTableware` |
| 6 | `create-order` | 创建团餐订单（本项目默认 dry-run） | 同上 + `addressId`、`remark` |

### 关键参数契约

```
beType = 6            企业团餐（1 到店 / 2 麦乐送 / 5 得来速 / 6 团餐）
orderType = 2         外送场景（团餐属于外送）
beCode                业务编码，与 storeCode 配对，由 delivery-query-stores 返回
gmServiceCode         助餐服务码，由 query-meal-assistance 返回，团餐必传
reservationDate       预约时间，格式 "yyyy-MM-dd HH:mm"
items                 [{"productCode": "901120", "quantity": 20}]
```

金额单位：**分**。`originalPrice`、`price`、`discount`、`enjoyable.balance` 全部为分。

## 三、调用时序

```
用户："明天中午 20 人，人均 40"
        │
        ├─(1) delivery-query-addresses ─────────────► 地址列表，取 addressId
        │
        ├─(2) delivery-query-stores(beType=6) ──────► 团餐门店 + reservationTimeOptions
        │        └─ parse_slots() 解析 "午餐(12:00至15:59)"
        │
        ├─(3) query-meals(reservationDate) ─────────► 菜单：分类 → 商品码
        │                                              join data.meals[code] 拿名称与价格
        │
        ├─(4) query-meal-assistance(reservationDate) ► 三种服务 + enable 标记
        │
        ├─(5) calculate-price × N  ◄── 配餐迭代（最多 7 轮）
        │        每轮：按比例生成候选 → 真实算价 → 反馈缩放价格上限
        │
        ├─(6) calculate-price × 3  ────────────────► 同一单跑遍 GMS001/002/003
        │        └─ 输出实付对比表 + 差额结论
        │
        └─(7) create-order（dry-run 默认）──────────► 只回显下单参数
```

平均一次完整决策调用 **15～20 次** Tool（配餐二分收敛占大头），全部为只读（除最后可选的 `create-order`），远低于 600 次/分钟限流。

## 四、踩过的坑（实测记录）

这些是实现过程中真实遇到的问题，也是本项目相对其他方案的差异所在。

**1. 团餐门店用常规接口查不到。**
`query-nearby-stores` 的 `beType` 枚举只有 `1`（到店自取）和 `5`（得来速），**没有团餐**。必须改用 `delivery-query-stores` 且传 `beType=6`，同时依赖一个已保存的 `addressId` 作为入口。这一步门槛导致了团餐场景长期空白。

**2. 团餐菜单必须带预约时间。**
同一门店，不传 `reservationDate` 返回 57 个商品，传 `2026-10-24 12:00` 返回 58 个——档期不同，可售集合不同。所有查询都应带上预约时间。

**3. `canWithOrder` 在团餐场景恒为 `false`。**
实测三家门店、多个时段，57/58/64/45 个商品的 `canWithOrder` 全为 `false`。推测与团餐"预约制 + 人工确认"有关，而非商品下架——因为同一批商品 `calculate-price` 能正常返回完整价格与折扣。因此本项目不以此字段做过滤，改用 `calculate-price` 作为可下单性的实际校验。

**4. 助餐服务可用性随预约时间变化。**
不传 `reservationDate` 时，`GMS002`（专人分餐）与 `GMS003`（专人摆台）返回 `enable: false` 且带 `unusableReason: "专人服务需提前4h预约"`；传入 16 小时后的时段，两者均变为 `enable: true`。这正是"提前 4 小时"规则的接口级体现，本项目把它做成了下单前的显式提示。

**5. 折扣与服务档次成反比。**
同一单（30 份麦香鱼，原价 ¥759）：保鲜速达 74 折 ¥541.80，专人分餐 84 折 ¥613.80，专人摆台 89 折 ¥649.80。服务越重，折扣越少。这个规律只能通过横向调用 `calculate-price` 三次才发现。

**6. 工具返回是「说明 + JSON」混合文本。**
不是纯 JSON，末尾还附一句展示要求。需要用 `json.JSONDecoder().raw_decode()` 从首个 `{"success"` 位置宽松解析，不能用 `json.loads` 整段解析。

## 五、业务价值

**直接省钱。** 20 人下午茶场景，选保鲜速达而非专人摆台，单次差 ¥160.50。按每周一次计算，一年 50 次约 **¥8,000**。这个数额不是靠"找优惠券"省出来的，而是靠把一个原本不可见的决策点暴露出来。

**降低决策成本。** 一场 20 人的团餐，行政需要决定：哪家店、哪个档期、点什么、要不要摆台、够不够吃、会不会超预算。这原本是 5～6 次来回沟通，现在一次对话给完整方案。

**规避执行风险。** 专人服务需提前 4 小时，临时下单会被接口拒绝或降级。提前校验把"下单失败"变成"下单前提示"。

**填补能力空白。** 麦当劳 MCP 已开放团餐相关能力，但团餐门店的发现路径隐藏在 `delivery-query-stores` 的非默认枚举里，缺少面向企业场景的上层封装。本项目是这块能力的第一个完整用例。

## 六、扩展方向

- 接入 `query-meal-detail` 解析套餐结构（`roundList`），支持"主食可换"的定制配餐
- 接入 `campaign-calendar`，把门店活动与新品尝鲜纳入配餐候选
- 多人偏好收集：把每人的忌口、辣度、素食需求作为约束求解
- 历史订单复用：基于 `order-list` 做"上次那单再来一次"，并对比当时的折扣是否仍有效
