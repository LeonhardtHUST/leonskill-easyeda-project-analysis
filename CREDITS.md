# 来源与致谢

本技能是在两个既有项目的基础上重新整理而成的。代码与文档均已按本仓库的实现重写，
未直接复制原项目的文件结构。

## easyeda-ngspice-sim

- 许可：MIT
- 用途：`.epro2` / `.epru` 解析的事实来源与算法基础

本仓库的以下内容源自该技能的解析层：

| 本仓库 | 来源 |
|---|---|
| `scripts/epro2.py` | 容器解包与 GBK 成员名还原 |
| `scripts/epru.py` | 记录流分段、按 ticket 归约 |
| `scripts/library.py` | 符号/器件/封装表构建、引脚坐标变换 |
| `scripts/schematic.py` | 页面 IR 与并查集连通性 |
| `scripts/pcbnet.py` | PCB 几何提取 |
| `references/container-and-records.md` | 记录类型表、坐标系与单位约定 |
| `references/pcb-geometry.md` | 叠层、过孔、板框字段与陷阱 |

该技能面向 ngspice 仿真，本仓库只取其中的工程文件解析部分，
去掉了仿真、网表生成、模型映射等与 CAD 数据解析无关的内容。

## dsh-eda-review

- 许可：Apache License 2.0
- 版权：Copyright 2026 chaser-star
- 项目：https://github.com/chaser-star/dsh-eda-review

本仓库从该项目吸收的是**方法论**（不含代码）：

| 本仓库 | 吸收内容 |
|---|---|
| `scripts/pcbnet.py` | 权威网表重建规则：无位号元件为删除残留、`PAD_NET` 按 (元件, 焊盘) 取最大 ticket、引脚 id 只在符号文档内唯一 |
| `scripts/epru_diff.py` | 两版语义 diff 的思路与噪声白名单（客户端 GUID、ticket 重编号、铺铜重灌、占位元素） |
| `references/netlist-rebuild.md` | 历史记录与最新数据的判定、DRC ≠ 电路正确 |
| `references/review-playbook.md` | P0/P1/P2 分级、"不使用某引脚≠可悬空"、官方库比对、数据手册引脚表偏移陷阱、单证据源不得下 P0 |

依据 Apache License 2.0 第 4 条，此处的署名与来源说明构成对该项目的归属声明。
该项目本身未包含 NOTICE 附加条款之外的限制。

## 格式事实的来源

`.epru` 是嘉立创EDA的私有格式，没有公开规范。本仓库中的字段含义来自对真实工程的
实际观察与交叉验证，并按"实测 / 推断 / 未验证"区分标注。格式随编辑器版本变化，
遇到解析失败率升高时应视为版本不匹配，而不是文件损坏。

EasyEDA、嘉立创EDA、立创EDA 是其各自权利人的商标。本技能与这些公司无隶属关系。
