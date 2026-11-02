# 容器与记录流

## 一、`.epro2` 是 ZIP

文件头 `50 4B 03 04`。解压后通常有：

| 成员 | 内容 |
|---|---|
| `project2.json` | 项目元数据（标题、编辑器版本） |
| `<工程名>.epru` | 核心数据，UTF-8 逐行记录流，几 MB |
| `IMAGE/*.webp` | 原理图/PCB 预览图，解析不需要 |

**成员名是 GBK。** Python 用 ZIP 头里的字节按 cp437 解出字符串，
需要 `name.encode('cp437').decode('gbk')` 才能还原中文；`decode_member_name()` 已按
GBK → UTF-8 → 原名 三级回退。

Windows PowerShell 5.1 的 `Expand-Archive` 只认 `.zip` 扩展名，直接解 `.epro2` 会报错；
要么先复制成 `.zip`，要么用 `zipfile` 直接读（推荐，省去落盘）。

旧版 `.epro` 是**目录**不是 ZIP，无法按本流程解析；需要在 EDA 里另存为 `.epro2`。

## 二、`.epru` 记录流

逐行，每行格式：

```
{record}||{payload}|
```

- `record`：`{"type","id","ticket","firstTicket"}`；
- `payload`：可选，元素数据；空串或 `""` 表示该元素已被删除；
- 行尾可能多一个 `|`，解析前先去掉；
- 没有 `||` 的行直接跳过并计入失败率。

### 归约规则

同一 `(type, id)` 会重复出现很多次——这是**编辑历史**，不是重复数据。
**ticket 最大的那条才是当前状态**。本技能的实现见 `epru.parse_records()`：
文档段内用字典 `(type, id) → (ticket, payload)` 边解析边覆盖。

`payload` 为 `None` 的记录表示删除，`doc_elements()` 会跳过。

### 文档分段

`DOCHEAD` 记录开启新文档段，载荷里带：

- `docType`：`SCH_PAGE` / `SIMULATION` / `SYMBOL` / `DEVICE` / `FOOTPRINT` / `PCB` /
  `BOARD` / `SCH` / `CONFIG` / `FONT` / `BLOB`
- `uuid`：文档 id
- `editVersion`

`DOCHEAD` 之后的记录都归属该段，直到下一个 `DOCHEAD`。没有 `DOCHEAD` 之前出现的记录丢弃。

### 常见记录类型

| type | 含义 | 关键字段 |
|---|---|---|
| `COMPONENT` | 元件实例 | `x` `y` `rotation` `isMirror` `attrs` `angle` `layerId` |
| `PIN` | 符号引脚（仅 SYMBOL 文档） | `x` `y` `rotation` `length` |
| `ATTR` | 属性 | `parentId` `key` `value` |
| `PAD_NET` | 焊盘-网络（仅 PCB） | id 为 `["PAD_NET", 元件id, 焊盘号, 焊盘id]`，载荷 `padNet` |
| `PAD` | 封装焊盘（仅 FOOTPRINT） | `num` `centerX` `centerY` |
| `LINE` | 线段 | 原理图：`lineGroup` `startX/Y` `endX/Y`；PCB：另加 `layerId` `width` `netName` |
| `WIRE` | 导线逻辑组 | id 即 `LINE.lineGroup` |
| `NET` | 网络定义 | id 为 `["NET", 网名]` |
| `VIA` | 过孔 | `netName` `centerX/Y` `holeDiameter` `viaDiameter` |
| `POLY` | 多边形 | `layerId` `polyType` `path` |
| `META` | 文档元信息 | `title` `docType`（数字）`attributes` |
| `LAYER` | 层定义 | id 为 `["LAYER", n]`，`layerType` `layerName` |
| `LAYER_PHYS` | 叠层物理参数 | `thickness` `er` `lossTangent` |

`META.docType` 是数字：`18` = 网络标识（NetFlag），`19` = 网络端口，
`20` = 图框/图纸符号。`library.component_type()` 据此把元件分成
`part` / `netflag` / `netport` / `sheet`。

### id 的形态

`id` 有时是字符串，有时是数组，有时是**数组的 JSON 字符串**（`["LAYER",1]`）。
一律先 `epru.norm_id()` 归一化再比较，否则 `int()` 会崩、字典查不到。

## 三、坐标与单位

| 对象 | 规则 |
|---|---|
| 原理图 | 1:1 逻辑坐标，无物理单位 |
| `.epru` 里的 y | 是 EDA 显示坐标的**负值**，读入后取负 |
| 旋转 | 文件 `rotation` 与显示值反向，变换时用 `-rotation` |
| 镜像 | 先 `x = -x`，再平移；顺序不能反 |
| PCB | **mil**，与 `CANVAS.unit` 写的 `mm` 无关 |

引脚绝对坐标 = 元件坐标 + `rot(-rotation) · (镜像后的符号本地坐标)`。

## 四、失败模式

| 现象 | 判定 |
|---|---|
| 文件头不是 ZIP | 旧版 `.epro` 或被截断的下载 |
| ZIP 里没有 `.epru` | 不是专业版工程，或文件不完整 |
| 记录行失败率 > 5% | 解析规则与文件版本不匹配，停止解析 |
| 某类记录整类缺失 | 该能力不可用（例如没有 `LAYER_PHYS` 就无法确认叠层） |
