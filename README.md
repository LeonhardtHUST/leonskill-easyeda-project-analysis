# leonskill-easyeda-project-analysis

给 AI 助手用的技能：把嘉立创EDA专业版（EasyEDA Pro / 立创EDA专业版）工程文件
`.epro2` 拆开、还原成可核对的结构化数据，并输出分析报告。

不需要打开 EDA，不需要联网，只依赖 Python 3.8+ 标准库。

## 能做什么

- 解包 `.epro2`（ZIP + GBK 成员名），读出 `.epru` 编辑记录流
- 按 ticket 归约出**当前状态**（而不是历史版本）
- 重建原理图侧网络：符号引脚坐标变换 + 并查集连通性
- 重建 PCB 侧权威网表：位号 / 焊盘号 / 引脚号 / 引脚名 / 网络
- 提取 PCB 几何：板框、层、叠层、走线长度与线宽、过孔孔径分布
- 逐脚比对两侧网表，标出不一致
- 对比两版工程，过滤掉保存噪声后给出真实改动
- 生成 `report.md`（P0/P1/P2 结构，数据缺失单独列）

## 快速开始

```bash
SKILL_DIR="$(pwd)"     # 或在安装后指向技能目录

python3 "$SKILL_DIR/scripts/analyze.py" yourboard.epro2 --out ./eda-out
cat ./eda-out/report.md
```

产物：

```
eda-out/
├── meta.json              工程概况与文档清单
├── schematic_P1.json      每页 IR（元件/引脚坐标/导线/网络）
├── nets.json              各页网络成员表
├── pcb_netlist.csv        PCB 权威网表（Excel 可直接打开）
├── pcb_geometry.json      板框/层/叠层/走线/过孔
└── report.md              分析报告
```

两版对比：

```bash
python3 "$SKILL_DIR/scripts/analyze.py" --diff old.epro2 new.epro2 --out ./eda-diff
```

## 安装到 WorkBuddy

仓库根就是技能根（`SKILL.md` 在顶层），仓库名与技能目录名完全一致，直接 clone 进技能目录：

```bash
cd ~/.workbuddy/skills
git clone https://github.com/LeonhardtHUST/leonskill-easyeda-project-analysis.git
```

得到 `~/.workbuddy/skills/leonskill-easyeda-project-analysis/`，重启会话即可。升级：

```bash
cd ~/.workbuddy/skills/leonskill-easyeda-project-analysis && git pull
```

**目录名必须是 `leonskill-easyeda-project-analysis`**：`easyeda-ngspice-sim` 的离线入口按这个路径查找依赖
（`scripts/analysis_dep.py` 的候选路径之一），改名会让它报 `DEPENDENCY_MISSING`。

不要用 `cp -r . <目标>` 安装：会把 `.git/`、`.workbuddy/`（本机会话记录）一起拷进技能目录。

之后交给助手一个 `.epro2`，说"解析这个工程"即可触发。

## 自测

```bash
python3 -m unittest discover -s tests -v
```

测试用合成工程（`tests/make_fixture.py`），不含任何真实工程数据。

## 目录

```
SKILL.md                          技能主体
skill-dependencies.json           能力声明、依赖与降级映射（机器可读）
references/output-contract.md     产物契约：schematic_*.json / pcb_geometry.json 的字段承诺
references/container-and-records  容器与记录流格式
references/netlist-rebuild        网表重建、diff、噪声过滤
references/pcb-geometry           PCB 几何与单位陷阱
references/review-playbook        分级、引脚核对、BOM 对齐
scripts/                          epro2 / epru / library / schematic / pcbnet / epru_diff / analyze
tests/                            合成工程 + 端到端断言
```

产物被 `leonskill-easyeda-ngspice-sim` 消费，字段承诺与变更流程见
[`references/output-contract.md`](references/output-contract.md)。

## 限制

- 实测覆盖 EasyEDA Pro 4.1.x；更早版本记录类型不同会主动报错
- 只读：不修改、不回写工程文件
- 不判断设计优劣。报告里的自动检查项只覆盖可机械判定的部分，
  引脚功能、封装选型、电流余量需要对照数据手册人工核对
- 无法解析旧版目录式 `.epro`

## 许可

MIT。来源与第三方致谢见 [CREDITS.md](CREDITS.md)。
