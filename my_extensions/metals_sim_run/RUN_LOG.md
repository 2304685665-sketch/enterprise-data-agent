# 模拟运行记录（工具层真实运行，模型层未运行）

## 一、公开资料检索（项目自带 BM25，无向量部分）

- 查询：五矿发展 营业收入 净利润
  - doc02_peer_minmetals_2025 / 经营概况：## 经营概况 五矿发展主要从事资源贸易、金属贸易、供应链服务三大类业务。2025 年报告期内实现营业收入 528.23 亿元，同比下降 2…
  - doc03_peer_zhejiang_zhongtuo_2025 / 摘要里没有的内容：## 摘要里没有的内容 页面没有给出营业收入、归母净利润总额和应收账款余额，财务表格是图片。…
- 查询：应收账款 信用减值 计提
  - doc03_peer_zhejiang_zhongtuo_2025 / 与应收账款风险相关的披露：## 与应收账款风险相关的披露 2025 年度公司计提信用减值损失 3,837.98 万元，计提对象包括应收票据、应收账款、其他应收款等。资…
  - doc03_peer_zhejiang_zhongtuo_2025 / 摘要里没有的内容：## 摘要里没有的内容 页面没有给出营业收入、归母净利润总额和应收账款余额，财务表格是图片。…
- 查询：为什么不做价格预测
  - doc05_paper_llm_trading_agents_survey / 对本方案的取舍：## 对本方案的取舍 这是本方案自己的判断，不是论文的结论：由于没有可靠证据说明智能体能稳定预测价格，方案把助手定位为信息整理和决策辅助，不…
  - doc01_imf_metal_prices / 数据是什么：## 数据是什么 铜、铝、锌的全球月度价格，序列号 PCOPPUSDM、PALUMUSDM、PZINCUSDM，单位美元/公吨，月度，未做季…
- 查询：智能体 供应链 风险 知识图谱
  - doc04_paper_agentic_supply_chain_risk / 开头：# 论文：供应链风险分析的智能体框架…
  - doc04_paper_agentic_supply_chain_risk / 做了什么：## 做了什么 把供应链网络当作知识图谱，用基于中心性分数的图遍历找出经济上最重要的风险路径，再由智能体协调图检索、数值因子表和新闻流，生成…
- 查询：上海期货交易所 今日铜现货报价
  - doc01_imf_metal_prices / 怎么用、不能怎么用：## 怎么用、不能怎么用 适合做趋势和同比环比的背景参考。它不是上海期货交易所或伦敦金属交易所的日度现货价，也不含升贴水和增值税，不能直接用…
  - doc05_paper_llm_trading_agents_survey / 开头：# 论文：金融交易中的大模型智能体综述…

## 二、台账与行情查询（sql_executor，真实运行）

- 行情环比/同比：aluminium | 3158.0 | -8.2 | 21.2 ; copper | 13543.0 | -0.1 | 38.6 ; zinc | 3594.0 | 1.6 | 30.1
- 逾期未收：客户B | 3840466.0 ; 客户A | 1288759.0 ; 客户D | 373831.0 ; 客户C | 194058.0
- 逾期超 90 天客户：客户B ; 客户C ; 客户D
- 库存成本与基准价：aluminium | 21500.0 | 21399.0 | -101.0 ; copper | 83531.0 | 91762.0 | 8231.0 ; zinc | 22449.0 | 24350.0 | 1902.0

## 三、边界测试

- 写入：SQL Error: only SELECT statements are permitted. Write operations are not allowed through 
- 删除：SQL Error: only SELECT statements are permitted. Write operations are not allowed through 
- 挂载外部库：SQL Error: only SELECT statements are permitted. Write operations are not allowed through 
- PRAGMA 写入：SQL Error: only SELECT statements are permitted. Write operations are not allowed through 
- 超出数据范围：Query executed successfully but returned no rows.
