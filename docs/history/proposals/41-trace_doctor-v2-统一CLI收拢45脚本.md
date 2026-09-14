# Proposal 41：trace_doctor v2 — 统一 CLI 收拢 45 个脚本 + 分层诊断

> 日期：2026-07-25
> 状态：设计（接受 r31 红方全部裁决 + 转向"整合"而非"新增"）
> 前置：proposal 40（v1 原设计） + r31（红方评审，指出 RTL bug + 方法论问题）
> 触发：**用户核心痛点**："当前脚本功能太松散，来几轮对话我和你就不知道是干啥的了"。
>       `scripts/` 已有 45 个脚本，历史阶段性产物大量沉积，agent 每次对话都在瞎找。
> **本次转向**：v1 想"新增 trace_doctor"叠加到 45 脚本上，会让问题**更糟**。v2 把 trace_doctor
>       定位为**统一 CLI 入口**——把 45 个脚本按用途归类，多数并入子命令，废弃冗余的，
>       用户/agent 从此只记住**一个入口**。

---

## 0. 一句话新目标

**`trace_doctor <subcommand>` 是唯一入口。45 个脚本被合并到 6 个子命令 + 20 个保留的原生
脚本；剩余 20+ 个历史脚本移到 `scripts/attic/` 归档不再用。任何 agent 打开对话看到
`trace_doctor --help` 就知道当前能做什么、按什么顺序做。**

---

## 1. 现状盘点：45 脚本分类

按用途逐条分类（打 tag，决定"合并/保留/归档"）：

### 1.1 硬件通路 / 采集前端诊断（合并入 `trace_doctor probe`）
| 脚本 | 作用 | 处置 |
|------|------|:---:|
| `hw_selftest.py` | TPIU AA/55 voltmeter，测通路 err% | **保留（子命令引用）** |
| `fpga_health.py` | 读 dbg_regfile，报错误码 | **保留（子命令引用）** |
| `pin_wire_check.py` | 引脚焊接连通性 | 合并→`probe wire` |
| `pin_wire_check_isolated.py` | 隔离版接线测试 | 合并→`probe wire --isolated` |
| `pin_multi_toggle.py` | 多引脚同步翻转 | **归档**（旧调试） |
| `pin_speed_scan.py` | 引脚 slew rate 扫描 | 合并→`probe pin-slew` |
| `check_pinla.py` | pin_la bit 健康检查 | 合并→`probe pin-la` |

### 1.2 IDDR / 采样相位（合并入 `trace_doctor tap`）
| 脚本 | 作用 | 处置 |
|------|------|:---:|
| `iddr_tap_sweep.py` | 扫 IDDR tap | 合并→`tap sweep` |
| `trace_ctrl.py set-tap/set-tap-lane/set-tap-clk` | 设 tap CSR | **保留（子命令引用）** |
| `perlane_idelay_cal.py` | per-lane IDELAY 校准（proposal 33，已撤销） | **归档** |
| `eyescan_read.py` | 眼图扫描读回 | 合并→`tap eye-read` |
| `eye_sweep_capture.sh` | 眼扫抓样 | 合并→`tap eye-sweep` |
| `eye_scan_42m.sh` | 42M 眼扫特定脚本 | **归档**（阶段性） |
| `sampling_guard.py` | 采样安全区判定 | 合并→`tap safe-zone` |

### 1.3 频率扫 / 冲频（合并入 `trace_doctor freq`）
| 脚本 | 作用 | 处置 |
|------|------|:---:|
| `freq_sweep.py` | 频率扫 | 合并→`freq sweep` |
| `freq_ceiling_sweep.py` | 找上限 | 合并→`freq ceiling` |
| `freq_push.sh` | 逐档冲频 | 合并→`freq push` |
| `freq_run.sh` | 跑单频点 | 合并→`freq run` |
| `freq_scan_oversample.sh` | 过采样扫 | **归档**（旧方法） |
| `hardarm_yield.sh` | 硬件锁定率评估 | 合并→`freq yield` |

### 1.4 MMCM 相位（合并入 `trace_doctor mmcm`）
| 脚本 | 作用 | 处置 |
|------|------|:---:|
| `mmcm_status.py` | MMCM 状态读 | 合并→`mmcm status` |
| `mmcm_phase_scan.sh` | 相位扫 | 合并→`mmcm phase` |
| `mmcm_phase_quality.sh` | 相位质量 | 合并→`mmcm phase --quality` |
| `mmcm_test_one.sh` | 单点测试 | 合并→`mmcm test` |
| `mmcm_q42.sh` | Q42 特定脚本 | **归档** |

### 1.5 抓样 / 解码（合并入 `trace_doctor capture`）
| 脚本 | 作用 | 处置 |
|------|------|:---:|
| `trace_ctrl.py rearm` | 触发采样 | **保留（子命令引用）** |
| `trace_dump.py` | 抓样 dump | **保留（子命令引用）** |
| `capture.sh` | 抓样脚本包装 | 合并→`capture snapshot` |
| `trace_run.sh` | 一键跑 | 合并→`capture run` |
| `trace_stream_rx.py` | 流式接收 | 合并→`capture stream` |
| `la_readout.py` | LA DDR 回读 | 合并→`capture la-dump` |
| `swo_dump_banked.py` | SWO 分区 dump | **归档**（非 ETM） |
| `swo_live_bridge.py` | SWO 实时桥 | **归档** |
| `swo_to_orbetto.sh` | SWO 转 orbetto | **归档** |

### 1.6 解码 / 验证（合并入 `trace_doctor decode`）
| 脚本 | 作用 | 处置 |
|------|------|:---:|
| `decode/opencsd_etm4_run.py` | ETMv4 解码 | **保留（子命令引用）** |
| `decode/verify_calls.py` | 调用边对 ELF | **保留（子命令引用）** |
| `decode/make_opencsd_snapshot.py` | 生成 opencsd 快照 | **保留（内部）** |
| `decode/etm_with_time.py` | 附加 FPGA 时间基 | **保留（子命令引用）** |
| `tpiu_testpattern_diff.py` | TPIU pattern 对比 | 合并→`decode tpiu-diff` |
| `walk_score.py` | tpiu deframe walk 评分 | 合并→`decode walk-score` |
| `v0_golden_check.py` | v0 golden 对拍（proposal 22） | 合并→`decode golden` |
| `decode.sh` | 解码脚本包装 | **归档**（被子命令替代） |

### 1.7 构建 / 烧录 / 配置（合并入 `trace_doctor build/burn/etm`）
| 脚本 | 作用 | 处置 |
|------|------|:---:|
| `build.sh` | FPGA 综合 | 合并→`build fpga` |
| `build_h743.sh` | H743 固件构建 | 合并→`build fw` |
| `program.sh` | openocd 烧固件 | 合并→`burn fw` |
| `etm_enable.sh` | 启 ETM cfg | 合并→`etm enable` |
| `etm_recover.sh` / `etm_recover2.sh` | ETM 恢复 | 合并→`etm recover` |
| `target/etm_enable_h743.cfg` | ETM openocd cfg | **保留（内部）** |
| `traceclk_setter.py` | TRACECLK 设置 | 合并→`etm set-tclk` |

**汇总**：45 个脚本 → **6 大类子命令** + **10 个保留原生脚本**（其他子命令内部调用）
+ **~15 个归档到 attic/**（历史阶段性产物、被替代的、非 ETM 相关）。

---

## 2. 统一 CLI 结构

```
trace_doctor
├── diag              # 一键诊断（原 proposal 40 主功能，L0-L8 分层探测）
│   ├── (default)     # 跑全套诊断
│   ├── --layer L4    # 只跑一层
│   ├── --fix xxx     # 自动修复选项
│   ├── --dump-all    # 打包上传
│   └── --json out.json
├── probe             # 硬件通路探针（1.1 组）
│   ├── voltmeter     # = hw_selftest quick
│   ├── health        # = fpga_health.py
│   ├── wire          # 接线连通性
│   ├── pin-slew      # 引脚 slew
│   └── pin-la        # pin_la bit 健康
├── tap               # 采样相位（1.2 组）
│   ├── set-clk N     # 设 clock IDELAY tap
│   ├── set-data N    # 设 data IDELAY tap（全 lane）
│   ├── set-lane L N  # 单 lane tap
│   ├── sweep         # 扫 tap 找最优
│   ├── eye-read      # 眼图读回
│   ├── eye-sweep     # 眼图完整扫描
│   └── safe-zone     # 采样安全区评估
├── freq              # 频率相关（1.3 组）
│   ├── run F         # 单频点
│   ├── sweep         # 扫多频点
│   ├── ceiling       # 找上限
│   ├── push          # 逐档冲频（150→400M）
│   └── yield         # 硬件锁定率
├── mmcm              # MMCM 相位（1.4 组）
│   ├── status
│   ├── phase [--quality]
│   └── test P
├── capture           # 抓样（1.5 组）
│   ├── snapshot      # one-shot 抓样
│   ├── run           # 一键抓+解
│   ├── stream        # 流式抓
│   ├── la-dump       # DDR3 LA 回读
│   ├── rearm         # 触发
│   └── status        # 读 FPGA 状态
├── decode            # 解码验证（1.6 组）
│   ├── opencsd F     # opencsd 解 + 覆盖统计
│   ├── verify F      # 调用边对 ELF（黄金判据）
│   ├── perf F        # 导出 Perfetto perf
│   ├── tpiu-diff     # TPIU pattern 对拍
│   ├── walk-score    # deframe 评分
│   └── golden        # v0 golden 对拍
├── build             # 构建（1.7 组）
│   ├── fpga [--top T] [--params K=V]
│   └── fw [--flags F]
├── burn              # 烧录
│   ├── fpga [BIT]    # 烧 FPGA bit
│   └── fw [HEX]      # 烧 STM32 hex
└── etm               # STM32 ETM 配置
    ├── enable [--bb N] [--stall N]
    ├── recover       # ETM 卡死恢复
    ├── set-tclk F    # 设 TRACECLK
    └── show          # 读 CoreSight 寄存器
```

**关键规则**：
- **每个子命令 <30 行**：主要是参数解析 + 调用内部函数或原生脚本；不重造轮子。
- **`trace_doctor <group> --help`** 列该组下所有子命令，一目了然。
- **`trace_doctor recent`**（额外）：显示最近 N 次运行历史，防止 agent 忘了刚做过什么。
- **状态文件** `~/workpath/orbcode/.trace_doctor.state.json`：记录当前烧的 bit、当前固件、
  当前 tap 值、上次抓样文件。agent 一进来读这个就知道现场状态。

---

## 3. 分层诊断（接受 r31 全部裁决）

### 3.1 接受 r31 的三条阻断点

**阻断 1（RTL）**：`gpio_clk_edge = gclk_s1 ^ gclk_s2` 在 100M+ TRACECLK 下欠采样。
→ **短期**：`trace_doctor diag` 的 L4 层**不采信 dbg_regfile 频率读数**，只用它做**定性
GPIO 翻转判据**（非零 / 零，二值），频率用**主机侧 CoreMark 分数间接验证**。
→ **长期**：修 RTL（`gpio_clk_edge` 改为 clktap MMCM 域采样后降频到 clk125 计数，或
toggle-FF 跨域）——单独作为 P0.5 任务，**不阻塞 P0 CLI 整合**。

**阻断 2（阈值实测校准）**：所有数值判据（"±5%"/"<20%"/"<0.5%"）先跑基线实测再定。
→ `trace_doctor diag --baseline`：在已知全绿态跑 10 次，记录每个数值判据的均值+方差，
自动生成 `.trace_doctor.baseline.json`，把阈值 = 均值 ± 3σ。后续 `diag` 用这个基线判 PASS/FAIL。
→ 现在没基线数据前，判据统一走**"trending"** 模式：只报数值，不 PASS/FAIL。

**阻断 3（早停对隐藏耦合失效）**：proposal 38 mem 基址 bug 类型的失效是"逐层绿、下层反查"。
→ `diag` 默认早停快速；**加 `--deep` 模式全跑 + 交叉验证**（opencsd 一路 + orbetto 独立
另一路，判据一致才算真绿）。文档明确"逐层绿≠整体绿，主诉失败必须 --deep"。

### 3.2 接受 r31 的其他修正

- **§1 失效模式清单**：把 r31 列的 5 条漏项加进：
  1. CoreMark 一轮 >3s 期间被 halt 打断 → banner 永远打不出（配套判据："`halt 到读取
     banner 间隔 >5s` 或 `banner 缺失但 PC 在 CoreMark 区" 就诊断为 halt 打断）
  2. openocd + openFPGALoader 同时跑抢占 USB → 明确列为"L0.co-contention"
  3. STM32 halt 期间 UART TX 缓冲丢 → `etm recover` 需重新配 UART
  4. make 判"无需重编" → `build fw` 默认加 `--force clean` 或读 hex 时间戳警告
  5. hgfs 半可用（能 ls 不能 read） → `test -r ...` + `stat -c %s` 检 size>0
- **§4.5 阈值来源**：`--baseline` 生成，取消拍脑袋。
- **§6 反例清单** 补 r31 建议的 4 条边界。
- **§9 遗漏资产** 补 `la_ddr_writer` 诊断寄存器（proposal 32）读取子命令 `capture la-dump`
  已收进 CLI。`v0_golden_check` 收进 `decode golden`。

### 3.3 状态文件设计（防止 agent 迷路的关键）

`~/workpath/orbcode/.trace_doctor.state.json`：
```json
{
  "last_updated": "2026-07-25T21:30:00",
  "fpga": {
    "bit_file": "trace_iddr_clktap.bit",
    "bit_md5": "9831ac82...",
    "build_id": null,          // dbg_regfile 未在此 bit
    "dbg_regfile": false,      // 主力 bit 缺
    "tap_clk": 24,             // 上次扫出的 clock IDELAY tap
    "tap_data": 12
  },
  "stm32": {
    "firmware_flags": "rt-300M-2bit",
    "elf_md5": "...",
    "hex_md5": "...",
    "hex_mtime": "2026-07-25T18:00:00",
    "pll_sysclk_mhz": 300,
    "pll_traceclk_mhz": 100,
    "cache_flag": "0x0000CACE",
    "coremark_iters_per_sec": 1223
  },
  "etm": {
    "bb": 0,
    "stall": 0,
    "tpiu_curpsize": "0x08",   // 4-bit
    "last_etm_config_time": "2026-07-25T21:20:00"
  },
  "network": {
    "iface": "ens33",
    "fpga_ip": "192.168.10.42",
    "last_udp_ok": "2026-07-25T21:29:00"
  },
  "last_captures": [
    {"file": "/tmp/c4good.bin", "time": "21:15", "config": "300M-BB0-cache-tap24"},
    ...
  ],
  "warnings": [
    "dbg_regfile edge counter is under-sampled at 100M+ (r31); freq reading unreliable"
  ]
}
```

**每个子命令跑完自动更新状态文件**。agent 每次对话一进来运行 `trace_doctor status` 打印
这个文件——**"当前烧的 bit 是啥、STM32 跑什么频、上次抓样在哪"一目了然**。

---

## 4. 实施顺序（v2 修订）

**P0（1.5-2 天，接受 r31 工作量修正）**：
1. 骨架：`trace_doctor` Python 入口 + argparse 子命令树 + 状态文件读写
2. **归档冗余脚本**：把 §1 标"归档"的 ~15 个脚本 `git mv` 到 `scripts/attic/`，README 记录
   为何废弃
3. **6 大子命令包装现有脚本**：`build/burn/etm/probe/tap/freq/mmcm/capture/decode` 都调
   原生脚本，不重造
4. `trace_doctor status` / `recent` 基础功能
5. `diag` 层级探测（外部探测，L0-L4 除 freq 数值判据）

**P0.5（0.5-1 天，可选并行）**：
- 修 RTL 边沿检测（clktap MMCM 域采样+降频）
- 综合 `trace_iddr_clktap_dbg.bit`（含修好的 dbg_regfile）
- 重扫 tap 校准

**P1（0.5 天）**：
- `diag --baseline` 生成实测阈值
- `diag --deep` 交叉验证模式
- `diag --json`

**P2（0.5 天）**：
- `--fix` 自动修复
- `--dump-all` bundle

**总**：P0 + P1 + P2 = 2.5-3 天（不含 P0.5 RTL 工作）。**比 v1 估计（1+0.5+1=2.5天）
增加 25%，符合 r31 "P0 至少 1.5-2 天" 的现实估计**。

---

## 5. attic/ 归档策略

被归档的脚本**不删除**，`git mv scripts/xxx.py scripts/attic/xxx.py`，在 `attic/README.md`
里记录：
```markdown
| 脚本 | 归档原因 | 替代 |
|------|---------|------|
| pin_multi_toggle.py | 旧调试遗留（proposal 前期） | trace_doctor probe wire |
| eye_scan_42m.sh | 42M 特定，主线已过 100M+ | trace_doctor tap eye-sweep |
| perlane_idelay_cal.py | proposal 33 已撤销 | trace_doctor tap set-lane |
| freq_scan_oversample.sh | oversample 已被 IDDR 替代 | trace_doctor freq sweep |
| mmcm_q42.sh | Q42 阶段性 | trace_doctor mmcm test |
| swo_*.py/sh | 非 ETM 主线 | 无（保留归档，SWO 有需要再复活）|
```

**agent 找不到某脚本时**：先 `trace_doctor <group> --help`；仍找不到 → `grep -r <name>
scripts/attic/` 看归档去向。**杜绝"这脚本干啥的？"卡壳。**

---

## 6. AGENT.md 更新（同步）

`AGENT.md §7 关键文件索引` 更新：
- 把 45 脚本索引压缩为 **"入口只有一个：`trace_doctor`"**
- 6 大子命令简介 + attic/ 位置
- 状态文件 `.trace_doctor.state.json` 说明
- 明确"agent 新对话时先跑 `trace_doctor status`"

这样后续 agent 一开始就不会迷路。

---

## 7. 反例（v2 追加）

- **不重造轮子**：子命令包装现有脚本，别重写。原生脚本内部逻辑 bug 修在原生脚本里。
- **不隐藏原生脚本**：`trace_doctor <cmd> --show` 打印它内部调用的原生命令，方便 agent 直接
  拷贝调试。
- **不吞异常**：子命令失败必须原样传出 subprocess exit code + stderr。
- **状态文件不做单一真相**：状态文件只是缓存，每次跑 diag 先**校验**（如烧的 bit md5 vs 状态
  记录）而不是全信；不一致时警告。
- **归档不是删除**：所有原脚本保留在 attic/，git 历史完整。

---

## 8. 结论表

| # | 项 | 决定 |
|---|------|------|
| 用户核心痛点 | 脚本松散 45 个 | ✅ 统一 CLI + 状态文件 + 归档 |
| r31 阻断 1 | RTL 边沿欠采样 | ✅ 短期定性判据 / 长期修 RTL（P0.5 独立） |
| r31 阻断 2 | 阈值拍脑袋 | ✅ `--baseline` 实测校准 |
| r31 阻断 3 | 早停掩盖耦合失效 | ✅ `--deep` 交叉验证模式 |
| r31 §1 漏 5 条 | 失效模式 | ✅ 补入 |
| r31 §9 漏资产 | la_ddr_writer / v0_golden | ✅ 收进 capture la-dump / decode golden |
| SLA "30 秒定位" | 无实测 | ✅ 改成"层级 + top-3 原因" |
| 工作量 | v1 估 2.5 天 | ✅ 修正 P0 至 1.5-2 天 |

---

## 一句话给未来的自己

**新对话第一件事：`trace_doctor status`。想干啥先 `trace_doctor <group> --help`。
永远别再问"这脚本干啥的"。**
