# Booking capacity audit v1

面向场地运营，审计预订占用而非传感器实测。输入显式horizon(start,end)，1..100个venues(id,capacity)，
最多20000条bookings(id,venue,start,end,quantity)。时间为0..10^12整数tick，horizon和booking必须正时长；
容量0..10^6整数，quantity为1..10^6，id唯一、venue引用完整。容量0合法表示不容许预订。
预订为[start,end)半开区间，先裁剪到horizon，空交集无贡献；同刻离开和进入不产生瞬间超容。
扫描事件边界，输出覆盖完整horizon的分段occupancy/excess，并合并相邻等占用段。
peak=max occupancy；over_capacity_ticks为excess>0的时长，excess_unit_ticks为超量×时长，
booked_unit_ticks为占用×时长。每个场地包括无预订场地也有完整报告。
样例hall峰值4、超容5 tick、超额5 unit-tick、总预订55 unit-tick；quiet全程0。
预订外部区间不扩展报告，不执行真实预约、取消、分配或按人去重；无持续服务或数据库。
