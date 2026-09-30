"""Portable local launcher; run with the project's virtual environment Python."""
import argparse
import os
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description='FinIntel local research agent')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--demo',action='store_true',help='Use synthetic BOSS materials; rules when no model is configured')
    args=parser.parse_args()
    if not 1<=args.port<=65535: parser.error('port must be between 1 and 65535')
    root=Path(__file__).resolve().parent
    sys.path.insert(0,str(root/'backend'))
    os.environ['FININTEL_PORT']=str(args.port)
    if args.demo:
        os.environ['FININTEL_DEMO']='1'
        import store
        store.apply_migrations()
        store.execute("INSERT INTO app_setting(key,value) VALUES('demo_mode','1') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        import pipeline
        # HTTP remains the same UI contract; this launch explicitly routes BOSS to synthetic data.
        original=pipeline.pipeline.start_task
        def demo_start(query,**kwargs):
            kwargs['source']='demo'
            return original(query,**kwargs)
        pipeline.pipeline.start_task=demo_start
    else:
        import store
        store.apply_migrations()
        store.execute("INSERT INTO app_setting(key,value) VALUES('demo_mode','0') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    import uvicorn
    import app
    if args.demo:
        print('DEMO: synthetic BOSS input; rule analysis if no model configured. X/XHS require real adapters.',flush=True)
    print(f'FinIntel: http://127.0.0.1:{args.port}/',flush=True)
    uvicorn.run(app.app,host='127.0.0.1',port=args.port)


if __name__=='__main__': main()
