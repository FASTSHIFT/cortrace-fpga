# 29 — 用源码级 OpenCSD 把故障定位到 nibble 组装

日期：2026-09-07

## 方法：OpenCSD 不再当黑箱

把 OpenCSD v1.8.3 作为子模块编进 cortrace（`third_party/opencsd`，源码 -g -O0 构建），
可 gdb 单步解码器状态机。断在 ETMv4 packet processor 的 A-sync 校验
（`trc_pkt_proc_etmv4i.cpp:401`，即 `size!=12 || lastByte!=0x80` 判 BAD_SEQUENCE[ASYNC]）。

## gdb 观察到的真相

BAD_ASYNC 触发时，`m_currPacketData.size()` 全是 3/4/5/6/9/10，**从不是 12**。dump
出错位置的实际字节，A-sync 区规律性畸形：

```
@2659  应 00x11 80，实得 00x9  06 00 80
@7307  应 00x11 80，实得 00x10 80 00 81
@13950 应 00x11 80，实得 00x8  0c 00 00
@15503 应 00x11 80，实得 00x9  02 00 80
```

真 A-sync 是 **11 个 0x00 + 0x80**。抓回来的却是 **9-10 个 0x00 + 一个小字节(0x02/0x06/0x0c) + ...**
——零游程短了 1-2 个，中间插进小字节。**这不是随机位翻转，是零游程里系统性地少了字节/被改。**

## 定位：不在芯片，在 FPGA→nibble 组装→deframe 这条 PC 侧链路

对照 **DAP 直读的 ETF golden dump**（etf_golden_raw.bin，直接从 STM32 芯片读，
完全不过 FPGA、不过 nibble 组装）：

| 来源 | 干净 A-sync (00x11+80) | 畸形 |
|------|----------------------|------|
| ETF golden（芯片直读） | **4** | 1（仅开头 16 个 0 的 padding，正常）|
| FPGA 抓 + Python deframe | 3 | **53** |

**芯片侧 A-sync 完全干净；FPGA 抓+deframe 后大量畸形。** 所以字节丢失/改动是在
**FPGA 采集 → nibble 组装 → deframe** 这条链路引入的，不在 ETM/芯片。

## 与 TPIU 图案"逐字节干净"如何自洽

TPIU 测试图案（AA/55、walking-1）是**静态重复**图案，每个 TRACECLK 边沿都相同，
即使 nibble 配对/相位略错，读出来还是 0xA5——**掩盖了 nibble 配对错误**。而**真实
ETM 数据 nibble 变化**，配对错一位就体现为字节错/游程短。A-sync 的长零游程正是
nibble 掉/重最敏感的地方。

所以嫌疑精确指向 **nibble 组装**（`recover_assemble` / `dsl_parse.assemble`）在真实
数据上掉/错配 nibble，而不是静态图案能测出的东西。这是 PC 侧我们自己的代码。

## 下一步
gdb/print 定位：畸形 A-sync 是否都发生在固定的 nibble 边界？是掉半字节（相位滑移）
还是 DDR 上/下沿配对错？直接在 recover_assemble 的 nibble 流上验证一个已知 A-sync
区的字节，和芯片 golden 对齐。
