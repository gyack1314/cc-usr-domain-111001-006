# 受结果上限约束的救援信息空间采集资料源

本项目保存本地合成资料及对应的来源站行为，供资料接入开发使用。内容取自虚构场景，不代表真实活动、人员、设施或设备参数。来源服务只模拟公开读取与指定的表单行为，不包含资料采集端。

## 环境与运行

使用 Python 3.11 及以上版本，仅依赖标准库。来源只监听本机地址，端口可以通过参数调整。两个终端分别启动不同端口可以表示两个独立来源。

```sh
python -m source_site --port 8090
```

`source_site/source.py` 管理来源响应，`source_site/__main__.py` 是 HTTP 适配层，`sources/` 保存原文与接口约定。状态只存在来源进程内，重启来源会恢复初始样本。`/__control` 和 `/__calls` 属于本地运维入口，资料接入端的正常操作不使用它们。运维端可以通过 POST `/__control` 提供 JSON 对象改变 phase 或更新 state。phase 列表与业务接口见下文，细节见 `sources/contract.json`。

- `GET /session`：开始只读revision会话
- `GET /points`：revision=r1&bbox=x0,y0,x1,y1&time=t0,t1，最多3项，count是截断前数量
- `coordinate`：整数坐标闭区间；整数逻辑时间左闭右开
- `phases`：["base", "expired"]
- `control_state`：generation加一会使旧revision失效

用 Python 的 HTTP 客户端推进来源状态，例如：

```sh
python -c 'import urllib.request; r=urllib.request.Request("http://127.0.0.1:8090/__control", data=b"{\"phase\":\"base\"}", headers={"Content-Type":"application/json"}); print(urllib.request.urlopen(r).read().decode())'
```

## 开发检查

以下检查覆盖来源协议、异常响应和真实本地 HTTP 启动。检查不会要求访问公网，也不会写入业务状态目录。

```sh
python -m unittest discover -s tests -v
python -m compileall -q .
```

容器环境也可以复现相同检查：

```sh
docker build -t spatial-harvest-source .
docker run --rm --network none spatial-harvest-source
```

来源的HTTP异常是合成协议行为，便于资料接入端开发和诊断。样本可扩展；业务数据、原始HTML以及接口响应不应被当作真实部署凭据或真实业务事实。

## 空间分片采集器（harvester）

`harvester/` 是资料接入端，针对“每次最多返回 3 条、没有翻页游标”的来源做确定性分片采集，并区分**确证采全**与**无法消除的截断**。仅依赖标准库，坐标均为整数逻辑坐标，不代表真实设施。

### 拆分策略

- 单元 = 整数闭区间 bbox `x0<=x<=x1, y0<=y<=y1` + 左闭右开时间窗 `t0<=updated_at<t1`，与接口语义一致。
- 每次选择**最宽维度二分**（同宽按 x→y→t）。空间按闭区间切成 `[lo,mid]` 与 `[mid+1,hi]`，边界点只归属一侧；时间按左闭右开切成 `[t0,mid)` 与 `[mid,t1)`。子单元互不重叠且并集恰等于父单元，因此不存在漏点，也不会把边界重复返回当成重复资料。
- 三个维度都退化为 1（单个整数空间点、时间宽度一）仍被截断时，判定为**不可解单元**：停止细分、记录缺口，绝不靠重复请求假装拿全。
- 顶层先沿空间长边切成 `--tiles` 个大区，调度时在大区之间**轮询**（每趟每个未完成大区至多发一个请求），高密度区域无法耗尽整轮预算；预算耗尽时队首单元原样保留为可续采的 pending 单元。
- 不依赖样本中的固定点位数或坐标：所有决策只基于单元宽度与来源返回的 `count/truncated`。

### revision 轮次与去重

- 每轮通过 `GET /session` 取得一个只读 revision 会话，一轮内只接受该 revision 的结果。来源返回 410 时该轮立即冻结为 `abandoned_expired`，**不宣告任何完整**；新会话开启全新轮次并整体重采，不同 revision 的空间片绝不拼成一次全量。
- 记录按 `source_id` 去重，较新轮次（更新 revision）的记录覆盖较旧记录；同一轮次重复见到同一编号只追加溯源，不制造重复资料。
- 请求预算按轮次配置，仅对 `/points` 查询计数；状态在每个单元处理后原子落盘。重启后对同一 `--out` 目录再次运行即从 pending 单元继续同一会话，扩大预算是追加而不是从头开始；若重启期间会话已过期，则自动转入新轮次。

### 运行

```sh
# 进程内直连仓库自带合成来源（隔离模式）
python3 -I harvest.py --bbox 0,0,10,10 --time 0,5 --budget 20 --tiles 4 --out state/run1
# 普通模式等价入口
python3 -m harvester --bbox 0,0,10,10 --time 0,5 --budget 20 --out state/run1
# 对接本地 HTTP 来源
python3 -I harvest.py --http-url http://127.0.0.1:8090 \
    --bbox 0,0,10,10 --time 0,5 --budget 20 --out state/run1
```

退出码：`0` 全部确证采全；`1` 预算耗尽但有可续采单元；`2` 存在不可解单元；`3` 来源协议故障（不落任何完整结论）。

输出目录包含：

- `state.json`：可续采状态（每轮每 tile 的队列、终态单元、完整请求链、catalog）。
- `report.json`：覆盖结论。`complete_cells` 是确证采全的最终单元（含每个单元的来源请求链与记录编号）；`unsolvable_cells` 是无法消除截断的原子单元及至少缺口数；`pending_units` 是可继续的未覆盖单元；`abandoned_rounds` 记录 revision 过期事件。
- `records.json`：按 `source_id` 去重后的记录，每条带 `_harvest` 溯源（轮次、revision、所属单元与单元状态）。

### 采集器自动检查

```sh
python3 -I run_checks.py        # 隔离模式，含来源原有检查与采集器检查
python3 -m unittest discover -s tests -v   # 仓库原有入口同样可用
```

检查包括：随机小范围数据与逐点穷举 oracle 对照（多组种子，证实正常区域无漏点、划分互不重叠且覆盖全范围）；极密同刻点被判定为不可解单元且绝不报为完整；小预算多次续采与一次性采集的有效请求数一致、不重开会话；重启期间 revision 失效时旧轮冻结、新轮重采且完整结论只含新 revision（无旧记录混入）；大区轮询的公平调度；以及仓库样本与真实本地 HTTP 的端到端流程。

## 开发检查

以下检查覆盖来源协议、异常响应和真实本地 HTTP 启动。检查不会要求访问公网，也不会写入业务状态目录。

```sh
python3 -I run_checks.py
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
```
