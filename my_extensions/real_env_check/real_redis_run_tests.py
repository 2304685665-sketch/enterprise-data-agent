import sys, runpy, redis.asyncio as ar, fakeredis.aioredis as fr, asyncio
class Real(ar.Redis):
    def __init__(self,*a,**k):
        super().__init__(host="127.0.0.1",port=6390,decode_responses=False)
        import redis as sync
        sync.Redis(port=6390).flushall()
fr.FakeRedis = Real
sys.argv=[sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
