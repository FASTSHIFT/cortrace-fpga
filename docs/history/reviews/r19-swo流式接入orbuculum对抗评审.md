# r19 · 提案 18「SWO 流式接入 orbuculum」对抗性评审

> 评审对象：`proposals/18-swo流式捕获接入orbuculum方案.md`
> 立场：红方，严格证伪。已读：`orbuculum/Src/orbuculum.c`（`-s/-T/-N/-t` + OFLOW/legacy 检测）、`fpga_core_net.v`（现有 TX 路径）、`swo_stream_top.v` 上下文、sidetrack README §10。
> 怀疑的地基：第 1 条 orbuculum 对接格式。

---

## 总裁决

**该改 + 阶段重排。地基（orbuculum 吃什么格式）没验证，被"orbuculum 支持 SWO-over-TCP"过度延伸成"我们的字节格式它一定能吃"。在地基用零成本钉死之前，阶段 2 的 FPGA 自发 TX（被严重低估）不该动。**

读完源码，三个硬结论：
1. **地基是"存疑"不是"成立"**：proposal §1 自己用"UART 解码它自己做 **/ 或** 我们已解的 TPIU 帧字节"这种**二选一的对冲**写法——这恰恰暴露作者**没确定 orbuculum 的 `-s` 源到底期望哪一个**。两个互斥假设并列不是"已查证"。
2. **自发 UDP TX 不是"echo 小变体"，被低估一个量级**：读 `fpga_core_net.v`，现有 TX **100% 由 RX 触发**，dest IP/port/MAC **全部来自收到的包**，没有任何定时器/自发 header/独立 ARP。自发推流要新造这一整套。
3. **丢包"不致命"是把离线重锁结论错误外推到实时流**——尤其在"高 baud sync 几乎不发"的前提下，丢一个 UDP 包可能让 orbuculum 长时间失锁。

最该先做、最低成本证伪地基的一步：**用一段已知正确的 UART-decoded TPIU 字节，喂 `orbuculum -s ... -T -N -t 2` 走 TCP，看它能不能解出 proj_add 的已知 PC**。这一步零 FPGA、零 RTL，直接判定整个提案地基成立与否。

---

## 逐条质疑

### Q1 orbuculum 接口格式（地基）

【结论：**存疑 —— 提案用"/或"对冲，等于没验证**】

**依据（读源码）**：
- `-s <host>:<port>`（`case 's'`）→ `_nwserverFeeder` → `streamCreateSocket` 连一个 **TCP server**，注释明写"typically used for things like J-Link **but can also be a legacy orbuculum session**"。所以 `-s` 是**连出去当 TCP client**，proposal 的 udp2tcp 桥当 TCP server 是对的方向。
- **但 orbuculum 有 OFLOW vs legacy 两套协议**，且**会自动判别**。文件源路径明确：读头 16 字节嗅探 `%%ORBFLOW1.0.0%%`（`OFLOW_SIG`）来决定 `usingOFLOW`。**网络 `-s` 源走的是哪种、是否同样按 OFLOW 签名/设备能力判别，proposal 没查清。** 若 `-s` 默认按 OFLOW 解、或按设备协商 OFLOW，而我们喂的是**裸 legacy TPIU 字节**，则 orbuculum 把它当 OFLOW COBS 帧解 → 全是 COBS error，**一个字节都解不出**。
- **`-T` 的真实语义**：源码 `useTPIU` = "Strip TPIU framing from input flows"，help 里还加了一句"(mostly not relevant)"。这说明在 **OFLOW 主流路径下 TPIU 解帧通常不需要**（OFLOW 自带通道）。`-T` 是给 **legacy 原始 TPIU 流**用的。**所以"-s + -T + -N"能不能吃我们的裸 TPIU 字节，取决于 `-s` 是否进入 legacy 模式**——这正是没验证的点。

**proposal 的逻辑裂缝**：§1 同时写了两个互斥命题——
- "orbuculum 的 SWO-over-TCP 期望 **NRZ bit 之上的字节流**"（= UART 后的字节，对）；
- "UART 解码 **它自己做** / 或我们已解的 TPIU 帧字节"（= 又说它自己做 UART，又说我们喂已解的，二者矛盾）。

**真相只可能是一个**：`-s` legacy 源吃的是**已经 UART 解码的原始字节流**（J-Link 就是这么喂的——J-Link 在探针里做 SWO UART 解码，TCP 上吐字节）。所以"我们喂 UART-decoded TPIU 字节 + `-T`"**大概率方向对**，但 proposal 没有把它和"OFLOW 自动判别会不会拦路"这件事钉死，且自己的措辞自相矛盾——**这叫存疑，不叫已查证**。

**最低成本验证（这就是该最先做的一步）**：
```
# 零 FPGA、零 RTL：
# 1) 拿 sidetrack 已抓的 / etm35lib 已验证的一段 UART-decoded TPIU 字节存成 raw.bin
# 2) 用 nc / 小脚本当 TCP server 把 raw.bin 吐到 :5555
python3 -c "import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',5555)); s.listen(1); c,_=s.accept(); c.sendall(open('raw.bin','rb').read()); ..."
# 3) orbuculum -s localhost:5555 -T -N -t 2   →  orbmortem -e proj_add.axf -t 2
```
- **若解出 0x08000f8c add / loop_sum** → 地基成立，格式确认（legacy + -T 路径吃裸 TPIU 字节）。
- **若全是 COBS/OFLOW error 或 0 输出** → 地基证伪，`-s` 在按 OFLOW 解，proposal 的"FPGA 出裸 TPIU 字节直喂 -s"整条不成立，要么 FPGA 侧得封 OFLOW（COBS+sig，工作量暴涨），要么改用别的源。

**成本：半天，0 RTL。这一步没过，后面 FPGA 全是空中楼阁。**

---

### Q2 自发 UDP TX 的真实复杂度

【结论：**证伪"echo 小变体" —— 被低估一个量级**】

**依据（读 `fpga_core_net.v`）**：现有 TX 是**纯 RX 触发的 echo**，逐行看：
- `tx_udp_hdr_valid = rx_udp_hdr_valid && match_cond` —— **TX 头的触发源就是 RX 头**。没有 RX 就没有 TX。
- `tx_udp_ip_dest_ip = rx_udp_ip_source_ip` —— **目的 IP 抄收到的包的源 IP**。
- `tx_udp_dest_port = rx_udp_source_port`、`tx_udp_source_port = rx_udp_dest_port` —— 端口全抄 RX。
- dest MAC：由 `udp_complete` 内部 ARP 解析，而 ARP 的触发也来自这条 RX→TX 回路。
- `tx_udp_length = rx_udp_length` —— **连长度都抄 RX 的**。

**自发推流要新造的（全是现在不存在的）**：
1. **一个不依赖 RX 的 TX 触发源**：FIFO 阈值/定时器产生 `tx_udp_hdr_valid` —— 现有逻辑里这个信号**焊死在 `rx_udp_hdr_valid` 上**，要拆开重接。
2. **自发的 dest IP/port/MAC**：固定常量 IP/port 可以，但 **dest MAC 要么硬编码（脆，PC 网卡/ARP 缓存一变就断）要么主动发 ARP request 并等 reply** —— 而 `udp_complete` 的 ARP 当前是被 RX 流"顺带"驱动的，**主动 ARP 是新路径**。
3. **自发 UDP 长度/header 字段**：现在全抄 RX，自发要自己算 `tx_udp_length`（取决于这包攒了多少字节）。
4. **不破坏现有 :5001/:5002 应答口**：现有 RX→TX echo 回路和新的自发 TX 要**共用同一个 `s_udp_hdr/payload` 输入端口**，两个生产者抢一个 udp_complete TX 输入 → 要做仲裁，否则自发包和应答包互相踩。

**这不是"echo 的小变体"，是新增一个独立的 TX 发起方 + ARP + 仲裁**。proposal §6 自己把它列为"最大风险"——**那它就不该在 §3 同时被描述成"现有 echo TX 的小变体"**。两处自相矛盾，乐观的那处（小变体）误导了工作量评估。

**最低成本验证**：先在**纯网络回归**（不接 SWO 前端）里做一个"FPGA 上电后定时自发 UDP 包到固定 PC IP:5555"的最小实现，PC 端 `tcpdump` 看是否收到、ARP 是否解析成功、且 :5001/:5002 应答**仍正常**。这步把"自发 TX"从"流式捕获"解耦验证。**成本：中等（这本身就是 proposal 低估的那块），但必须在阶段 2 前单独验。**

---

### Q3 丢包的真实后果

【结论：**证伪"不致命" —— 把离线重锁结论错误外推到实时流**】

**依据**：
1. **流式下丢 1 个 UDP 包 = 丢一段连续字节**（典型 1024B）。TPIU formatter 帧是 16 字节对齐的连续流，丢 1024B = 丢 64 个帧的对齐 + 中间所有 I-sync 锚点。
2. **与"高 baud sync 几乎不发"叠加致命**：r17/sidetrack 已实测——baud 越高、stall 越狠，**full-sync/I-sync 越稀疏**。`-N`（tpiuKeepSync）的作用是"锁定后保持锁定"，**但它需要一次成功 sync 来建立锁**。丢包后若错位，重新对齐**要等下一个 full-sync**——而 sync 本就稀疏，**可能几十 ms~更久没有锚点，这段指令流全丢**。
3. **离线 vs 实时的本质差异**：离线 `etm35lib` 重锁 walk 是在**完整无损的字节流**上做（UDP 应答式分页读出**幂等、可重读**，doc 14 §31 实测 0.000%）。**实时流的 UDP 包丢了就没了，不可重读**——把"离线完整流上能重锁"的结论搬到"实时有损流"，是无效外推。proposal §5 那句"丢包不致命，trace 本就允许 unknown%，且我们有重锁 walk"——**重锁 walk 是离线能力，实时丢包是另一回事**。

**最低成本验证**：在 Q1 的纯软件实验里，**人为往 raw.bin 里挖掉几段 1KB**，喂 orbuculum，量化"丢 1 包导致多少指令流失锁/unknown"。若一个洞导致后面长段 unknown 直到下一个稀疏 sync → 证明丢包对实时流确实伤害大，需要在 FPGA 侧做**可靠传输或至少丢包可观测 + sync 频率提升**，不能当"不致命"。成本：0 RTL。

---

### Q4 方案 A vs 更简单的文件/管道路径

【结论：**存疑 —— udp2tcp + TCP server 这层可能为"像 ORBTrace 的 -s"而过度设计**】

**依据**：
- orbuculum 有 **`-f <file>`** 源。proposal 说它"仅离线回放"——**但 Unix 下 `-f` 可以喂命名管道（FIFO）**：`mkfifo swo.pipe; orbuculum -f swo.pipe -T -N -t 2`，另一端 `udp_recv.py > swo.pipe` 持续写。**这样省掉 TCP server 这一层**——PC 端只需 `recvfrom UDP → write(pipe)`，比"udp2tcp + 维护 TCP server + orbuculum 当 TCP client"简单。
- **但有个真问题要先查**：`-f` 文件源会 **嗅探 OFLOW 签名**（源码 §1479-1484：读前 16 字节比对 `%%ORBFLOW1.0.0%%`）。若不是 OFLOW 签名就按 legacy 解——这对我们裸 TPIU 字节**反而合适**（legacy + -T）。**所以 `-f` + 命名管道很可能比 `-s` 路径更简单且更可控**，proposal 没评估就否决了 `-f`。
- proposal 选 `-s` 的潜在动机是"orbuculum 原生支持 SWO over TCP，像 ORBTrace"——**这是"像参考实现"的吸引力，不是"最简路径"的论证**。

**最低成本验证**：Q1 的实验**同时跑两条**——一条 `-s` + TCP server，一条 `-f` + 命名管道，**喂同一段 raw.bin**，看哪条更省事、哪条 OFLOW 判别不拦路。**成本：Q1 基础上加 10 行，0 RTL。** 很可能命名管道路径直接胜出，阶段 2 的 udp2tcp 都不用写。

---

### Q5 战略与必要性

【结论：**存疑（倾向"更爽的体验"而非"项目需要的能力"）**】

**依据**：
- proposal 自己列：**已能离线解出真实 PC + 调用栈 + FPGA 时间戳**（doc 14/r14 实测）。实时 orbmortem 相对它，多解决的是**"边跑边看反汇编/热点"的交互体验**，不是**一个离线拿不到的结论**。
- 工作量（连续 FIFO CDC + 自发 TX + ARP + 仲裁 + PC 桥 + 对拍）**不小**（Q2 已证自发 TX 被低估），且 r16/r17/r18 已确认两个更该做的事悬而未决：**① 并口 SI 命门（重启崩 15%）② SWO IDDR 双沿前端的 baud 上探**。
- **这块工作挤占主线**：实时流式是"采集已通"之后的呈现层增强，**不在关键路径上**。

**判断**：实时流式属于"锦上添花"。在并口 SI 命门未解、SWO 前端 baud 上限未实测之前，把工时投到"让已能离线解的东西实时显示"，是**优先级倒挂**。

**最低成本"验证必要性"**：问一句——"实时 orbtop 能回答哪个离线 etm_with_time + Perfetto 回答不了的项目问题?" 若答不出具体的、项目需要的能力（而只是"更爽"），战略上应降级排后。

---

### Q6 阶段划分的诚实性

【结论：**证伪"阶段1零风险验证 orbuculum 链路" —— 用已知有缺陷的数据源验证，结论不可信**】

**依据**：
- proposal 阶段 1 用**方案 C（轮询 re-arm 拼流）**当数据源喂 orbuculum，号称"零 FPGA 风险验证 orbuculum 链路"。
- **但 proposal 自己在 §3 方案 C 里写明**：re-arm 每次清零、**有间隙**、"21M 下 re-arm 间隙会漏 sync"、"不是真流式"。
- **用一个已知会漏 sync、有间隙的数据源**去验证"orbuculum 能不能实时解我们的流"——**两种结果都没意义**：
  - 若解不出：**分不清是 orbuculum 接口格式错（地基问题）还是方案 C 的间隙/漏 sync 导致**（混淆变量）。
  - 若勉强解出一些：那是在**有缺陷的流**上的结果，**不能证明阶段 2 真流式（不同的字节连续性/时序）也能解**。
- **阶段 1 看似通过，阶段 2 真流式很可能暴露 Q1 的格式问题或新的时序问题**——因为阶段 1 根本没有干净地隔离"orbuculum 格式"这个变量。

**正确的阶段 1**：不用方案 C 的脏流，用 **Q1 的干净 raw.bin（已知 etm35lib 能 0% 解）** 喂 orbuculum。**干净数据 → 唯一变量是 orbuculum 接口格式**。这才叫"零风险解耦验证下游"。proposal 把脏数据源（方案 C）和接口验证混在一起，**变量没隔离**。

**最低成本修正**：阶段 1 的数据源从"方案 C 轮询脏流"换成"Q1 的干净 raw.bin"。0 成本，且立刻把地基钉死。

---

## 各条小结

| # | 质疑 | 结论 | 一句话 |
|---|------|------|--------|
| Q1 | orbuculum 格式地基 | **存疑** | "/或"对冲=没验证；`-s` 走 OFLOW 还是 legacy 没查清，OFLOW 自动判别可能让裸 TPIU 字节全 COBS error |
| Q2 | 自发 UDP TX | **证伪"小变体"** | 现有 TX 100% RX 触发、dest 全抄收到的包；自发要新造触发+ARP+仲裁，低估一个量级 |
| Q3 | 丢包不致命 | **证伪** | 实时丢包不可重读 + 高 baud sync 稀疏 = 失锁长段丢流；离线重锁结论错误外推 |
| Q4 | 必须 udp2tcp+TCP？ | 存疑 | `-f` + 命名管道很可能更简单，proposal 没评估就否决；选 -s 是"像 ORBTrace"的吸引力 |
| Q5 | 战略必要性 | 存疑 | 已能离线解出 PC+调用栈+时间戳；实时是体验增强，挤占并口 SI / SWO baud 主线 |
| Q6 | 阶段1诚实性 | **证伪** | 用已知漏 sync 的方案 C 当数据源验 orbuculum，变量没隔离，结论不可信 |

---

## 总评：执行 / 改 / 砍

**判定：改 + 重排，不立即执行 FPGA 部分。**

**正确顺序（成本由低到高）**：
1. **【先做这个，半天，0 RTL】钉死地基（Q1+Q4+Q6 合一）**：拿 etm35lib 已验证 0% 解的干净 raw.bin，**同时**喂 `orbuculum -s`(TCP) 和 `orbuculum -f`(命名管道)，加 `-T -N -t 2`，看能否解出 proj_add 已知 PC。
   - 解不出 → 地基证伪，提案砍/大改（FPGA 要封 OFLOW，工作量翻倍，可能不值）。
   - 解出 → 地基成立，且顺便选出更简单的源（很可能是 `-f` 管道，省掉 udp2tcp）。
2. **【再做这个，0 RTL】丢包压测（Q3）**：往 raw.bin 挖洞，量化丢 1 包的失锁代价，决定要不要可靠传输/提 sync 频率。
3. **只有 1、2 都过，才考虑 FPGA 阶段**：且阶段 2 的"自发 UDP TX"必须**先在纯网络回归里单独验**（Q2），不和流式捕获耦合。
4. **战略上**：把这整条排在并口 SI 命门 + SWO IDDR baud 实测**之后**（Q5）。

**结论反转前提**：
- 若步骤 1 证明 orbuculum 直接吃我们的字节、且 `-f` 管道路径成立 → 阶段 1 几乎零成本拿下实时 orbmortem，**那"先做阶段 1 体验验证"是合理的低成本里程碑**（但 FPGA 自发 TX 仍排在主线后）。
- 若步骤 1 证伪 → 提案砍。

---

## 点破自我说服处（直接说）

1. **把"orbuculum 支持 SWO-over-TCP"延伸成"我们的字节格式它一定能吃"**——这是地基上的过度延伸。orbuculum 支持 TCP 源是事实，但它**按 OFLOW/legacy 自动判别**、`-T` 还标着"mostly not relevant"，**我们的裸 UART-decoded TPIU 字节走哪条解码路径根本没验**。§1 用"它自己做 UART **/或** 我们已解的 TPIU 帧字节"这种二选一写法，正是没想清楚的痕迹——**两个互斥假设并列，等于零个已验证假设**。
2. **"自发 UDP TX 是现有 echo TX 小变体"**——读了 `fpga_core_net.v` 就知道现有 TX 的每一个字段都抄自收到的包，自发推流是从无到有造一个 TX 发起方 + ARP + 仲裁。§6 自己把它列为"最大风险"却在 §3 叫它"小变体"，**乐观的措辞掩盖了真实工作量**。
3. **"丢包不致命，有重锁 walk"**——重锁 walk 是在**离线、幂等、可重读**的完整流上验证的（doc 14 §31 的 0.000% 正是靠"丢包重读同一 base"），实时 UDP 丢了就没了。**借离线的鲁棒性给实时有损流贴金。**
4. **阶段 1 "零 FPGA 风险验证 orbuculum"**——却用已知漏 sync 的方案 C 当数据源，**没有隔离"orbuculum 格式"这个唯一该验的变量**。真正零风险的验证是喂干净 raw.bin，而那根本不需要方案 C，也不需要等任何 FPGA。**"分阶段、风险隔离"的说法下，第一阶段的变量其实没隔离干净。**

> 一句话：提案的下游链路方向（UDP 推流 + 桥 → orbuculum）大体合理，但它把"orbuculum 能吃我们的格式"这个**没验证、还自相矛盾的对冲假设**当成了地基，又把"自发 TX"这个**从无到有的新发起方**说成"echo 小变体"。先花半天用干净 raw.bin 同时试 `-s` 和 `-f` 管道把地基钉死——这一步成本最低、最能一票证伪整个提案；过了再谈 FPGA，且自发 TX 要单独验、整条排在并口 SI 命门之后。
