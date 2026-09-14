# 28 — 周期性检验 + 解码 desync 定位（破局进展）

日期：2026-09-07

## 方法（不信任何一方，看数据自身周期性）

中断已关（PRIMASK=1），workload 是固定的、数据无关的循环
（det_iter→8×node→leaf），所以每次 iterate 产生的 ETM **必须**逐周期相同。用它做
不依赖解码/spec 的独立检验。

## 关键纠正（读了 refs/IHI0064H ETMv4 spec 之后）

之前"解码出的 N atom 比朴素黄金多 6.7 倍 → 解码有 bug"的判断**是错的**。spec 6.4：
Atom Format 4/5/6 把连续 taken 分支打包，且**格式自带 N**（F4 `00`=N,E,E,E；
F5 `101`=N,E,E,E,E）。所以 N atom 是编码格式产生的，不是"分支没跳"。朴素 E/N 计数
不能用来判对错。**这不是解码 bug。**

## 周期性检验结果

- **原始字节**：46.5% 是 TPIU HSYNC 填充，自相关最高 62.6%@lag=1024（是 UDP 包结构/
  填充的周期，不是循环周期）。原始层被填充淹没，测不出循环周期。
- **A-sync 间距**：完全不规则（132/153/158/169/184/222…每个都不同）。清流里 A-sync
  由 TRCSYNCPR 按字节计时插入，间距应大致规则；这里毫无规律。
- **deframe 后 ETM top bytes**：0xf7(197k, Atom-E) / **0x96(179k, Address-Short-IS1)** /
  0x97(60k, Address-Short-IS0) / 0xf6(38k, Atom-N)。约 **每个 atom 配一个 address 包**
  = BB=1 的特征（每分支广播地址），确认 BB 确实开着。

## 解码 desync 定位（这是真进展）

解码 lister 显示：
- **只解出 91 个 INSTR_RANGE，却有 890 个 NO_SYNC/bad-packet 标记**（gap=0 密集）。
- 但解出来的 91 个 range **地址全对**：0x8011420(leaf_add)、0x8011438(leaf_xor)、
  0x8011452/60/68(node)、0x8011486/8c(det_iter body)——正是 selftrace 函数，计数合理。

**结论：trace 内容是真实正确的，解码器能正确解出短促的循环片段（PC 全对），然后撞上
坏包 desync，反复如此。** 不是内容错，是**周期性的包流破坏**让解码器每次只能解几个
range 就脱轨。

## 可疑点：0x96 短地址包解成越界地址

`I_ADDR_S_IS1 [0x96 ..]` 解出的地址是 **0x0801F62C / 0x0801F68E** 等 0x801Fxxxx——
**远超出 selftrace 代码区（0x8011xxx）**。0x96 短地址包极多（179k），但解出越界地址，
高度可疑：要么是 snapshot 的 ETM 配置（BB/地址位数/IS 模式）没和固件对齐导致地址包
被错误解释，要么是字节流里 0x96 附近有周期性错位。

## 下一步
1. 核对 snapshot 的 ETMv4 寄存器（TRCCONFIGR 已传 0x9，但 TRCIDR2 地址位数 IAS、
   IS0/IS1 模式等可能也要对齐），排除"配置不符导致 address 包错解"。
2. 若配置对齐后 0x96 仍解成越界地址，则是字节层周期性错位——用周期性检验定位错位
   的固定间隔。

## 工具
- `decode/golden_selftrace.py` — 从反汇编推一次 det_iter 的元素序列（59 分支，
  但 atom 打包后字节不同）。
- `decode/period_check.py` / `pc_period.py` — 周期性检验（字节层 / PC 序列层）。
