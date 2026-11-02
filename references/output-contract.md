# 产物契约（消费方：`leonskill-easyeda-ngspice-sim`）

本 skill 是**生产方**，`leonskill-easyeda-ngspice-sim` 的离线入口是**消费方**。
它只读本文列出的文件、只依赖本文列出的字段。**本文没写的字段不属于契约**，
改了不保证通知。

契约版本：`1.3.x`（对应 `SKILL.md` 的 `version`）；消费方在 `skill-dependencies.json`
里用 `applies_to_version` 记录它适配的版本。

## 0. 稳定性承诺

- `1.x` 内：**只增字段，不改名、不改单位、不删字段**。
- 破坏性变更（改名 / 删字段 / 单位或坐标口径变化）必须升 `minor` 以上，并在本文末尾
  的变更记录里写清迁移方式；老字段至少保留一个 `patch` 版本。
- 消费方必须能容忍**新增字段**（忽略未知键），不得因为多出字段而报错。

## 1. `schematic_<页名>.json`

每页原理图一个文件，页名经 slug 化；同名页会加 `_2` 后缀。顶层：

| 字段 | 类型 | 说明 |
|---|---|---|
| `project` | string \| null | 工程名 |
| `page` | object | `{uuid, name, docType}`，`docType` 用于区分原理图/符号/图页 |
| `components` | array | 见下表 |
| `wires` | array | 见下表 |
| `nets` | array | 网络表，当前消费方不读，留作扩展 |
| `warnings` | string[] | 解析期告警，消费方应原样带给用户 |

`components[]`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 记录 id |
| `componentType` | string | `part` / `netflag` / `netport` / 其他 |
| `designator` | string \| null | 位号；无位号的残留元件为 null（消费方应跳过） |
| `deviceTitle` | string \| null | 器件名，消费方在缺 `name` 时用它兜底 |
| `manufacturerPart` / `value` | string \| null | 型号与参数值 |
| `x` / `y` | number | 元件位置，**y 已做 Y 轴翻转** |
| `rotation` / `mirror` | number / bool | 摆放角度与镜像 |
| `pins` | array | `{number, name, x, y}`；**不提供 per-pin 网络**，网络由消费方按连通性算 |
| `attrs` | object | 原始属性键值（`Designator` / `Value` / `Name` …） |
| `net` | string | 仅 `netflag` / `netport` 有：该符号代表的网络名 |

`wires[]`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 记录 id |
| `line` | number[4] | `[x1, y1, x2, y2]`，y 已翻转 |
| `net` | string | 导线自带网络名；可能为空串，空串**不等于**未连接 |

坐标口径：沿用工程原始坐标单位（原理图 10 mil / 单位），本 skill **不做 mm 换算**。

## 2. `pcb_geometry.json`

多 PCB 文档工程的走线/过孔/铜层计数**跨文档累加**，板框逐板列在 `outlines`。
顶层：

| 字段 | 类型 | 说明 |
|---|---|---|
| `unit` | string | 恒为 `"mil"`；消费方按此换算 |
| `outline` | object \| null | 所有板框的并集：`{min_x, max_x, min_y, max_y, width_mil, height_mil, width_mm, height_mm, boards}` |
| `outlines` | array | 逐板框：`{min_x, max_x, min_y, max_y, width_mil, height_mil, width_mm, height_mm, doc}` |
| `layers` | array | `{id:int, type:string \| null, name:string \| null}` |
| `stackup` | array | `{id, raw}`，`raw` 是 `LAYER_PHYS` 原始记录（`zIndex` / `material` / `thickness` / `permittivity` / `lossTangent`） |
| `segments` | array | **逐段**走线，见下 |
| `traces` | array | 按网络汇总的统计口径，**与 `segments` 不要混用** |
| `vias` | array | `{net, x, y, hole, diameter, type}` |
| `pads` | array | 焊盘绝对坐标，见下 |
| `via_hole_histogram` | object | 孔径 → 数量 |
| `copper_layers_used` / `copper_layers_declared` | int | 铜层统计 |
| `segment_count` / `pad_count` | int | 图元计数 |
| `warnings` | string[] | 解析期告警（如「没有 LAYER_PHYS 记录，叠层无法确认」） |

`segments[]`：`{net, layer_id, x1, y1, x2, y2, width}`，单位 mil，端点未经拓扑归并。

`pads[]`：`{component, pin, net, x, y, layer_id, w, h}`。`x/y` 是**已算好的绝对坐标**
（元件坐标 + 旋转后的封装本地坐标）；`net` 可能为空串，表示该焊盘未接网络。

`outlines[].min_x/max_x/min_y/max_y` 是**必须存在**的字段：消费方靠它算板级范围。
只给宽高不够——没有原点就没法定位。

## 3. 消费方义务

1. **只读**：不修改、不回写、不移动产物文件。
2. **缺字段就降级并出声**：例如拿不到板框范围时，不得用走线范围冒充板子尺寸，
   必须退回并附加一条 warning 说明口径。
3. **单位自己换**：一律按 `unit` 读 mil，mm 换算由消费方负责，不要假设生产方给 mm。
4. **不依赖未列字段**：看到本文没有的键，可以忽略，但不能当成稳定契约使用。
5. **版本自检**：能读到本 skill `SKILL.md` 的 `version` 时，与自己的
   `applies_to_version` 比对，不匹配就提示用户。

## 4. 变更流程

| 角色 | 动作 |
|---|---|
| 生产方 | 改字段 → 同步改本文 + `SKILL.md` 版本；破坏性变更在下方「变更记录」写迁移 |
| 消费方 | 升级依赖前读本文；发现新字段只用不依赖；发现缺字段按第 3 节降级 |
| 双方 | 端到端冒烟：`make_fixture.py` 造工程 → `analyze.py` 出产物 → 消费方 `pcb_geometry.raw_from_analysis()` 读一遍 |

## 5. 变更记录

| 版本 | 日期 | 变更 | 对消费方影响 |
|---|---|---|---|
| 1.3.1 | 2026-11-02 | `outlines[]` 与 `outline` 补 `min_x / max_x / min_y / max_y` | 修复型：此前消费方按 min/max 过滤导致板框恒为空，`extent` 退化成走线范围。老字段全保留，消费方可直接受益 |
| 1.3.0 | — | `pcb_geometry.json` 增加 `segments[]` / `pads[]` / `pad_count` / `segment_count` | 增量，无破坏 |
