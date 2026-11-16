"""Read-only local clock diagnosis; never starts services or changes time."""
import argparse
import csv
from datetime import datetime,timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

from app.ecb_observation_support import atomic_json

DOCUMENTATION='https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings'


def parse_samples(text):
    samples=[]
    for row in csv.reader(text.splitlines()):
        if len(row)!=5:continue
        try:
            # /rdtsc emits three integer counters followed by delay and offset.
            counters=[int(value.strip()) for value in row[:3]]
            delay,offset=map(float,(row[3].strip(),row[4].strip()))
        except ValueError:continue
        if (any(value<0 for value in counters) or not all(map(math.isfinite,(delay,offset)))
                or delay<0 or delay>5):continue
        samples.append({'round_trip_seconds':delay,'offset_seconds':offset})
    return samples[:3]


def clock_check(*,probe_ntp=False,runner=subprocess.run,platform=os.name):
    started=datetime.now(timezone.utc).isoformat(); counter=time.monotonic()
    result={'mode':'ecb_observer_clock_check','started_at':started,'state':'not_verified',
            'service_query':None,'samples':[], 'clock_or_service_changed':False,
            'clock_sync_verified':False,'documentation':DOCUMENTATION}
    if platform!='nt':
        result['reason']='windows_diagnostic_not_available'
    else:
        commands=[['w32tm','/query','/status']]
        if probe_ntp:
            commands.append(['w32tm','/stripchart','/computer:time.windows.com','/period:2','/samples:3','/rdtsc'])
        for index,command in enumerate(commands):
            try:
                completed=runner(command,capture_output=True,timeout=25,
                                 encoding='mbcs',errors='replace',check=False)
                text=(completed.stdout or '')+'\n'+(completed.stderr or '')
                evidence={'command':command,'returncode':completed.returncode,
                          'error_codes':list(dict.fromkeys(re.findall(r'0x[0-9A-Fa-f]{8}',text)))}
                if index==0:result['service_query']=evidence
                else:
                    result['ntp_probe']={**evidence,'host':'time.windows.com','unauthenticated_reference':True}
                    result['samples']=parse_samples(text) if completed.returncode==0 else []
            except (OSError,subprocess.TimeoutExpired) as error:
                result['service_query' if index==0 else 'ntp_probe']={
                    'command':command,'state':'unavailable','error_type':type(error).__name__}
        samples=result['samples']
        if len(samples)==3:
            maximum=max(abs(s['offset_seconds']) for s in samples)
            result.update(state='offset_estimate_only' if maximum<=2 else 'large_offset_estimate',
                          maximum_absolute_offset_seconds=maximum)
        else:result['reason']='insufficient_valid_ntp_samples'
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    result['elapsed_seconds']=round(time.monotonic()-counter,3)
    result['notes']=[
        '少量单一未认证NTP参照只能估计偏移，不证明持续校时，也不验证来源公布时间。',
        '不调用/config、/resync、服务启动或系统时间修改；失败保留未知。']
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='只读校时诊断，不修改时钟或启动服务')
    parser.add_argument('--probe-ntp',action='store_true',help='额外对 time.windows.com 做3次有界偏移查询')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.output.exists():parser.error('Output already exists; choose a new evidence file')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    result=clock_check(probe_ntp=args.probe_ntp)
    atomic_json(args.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['state']=='offset_estimate_only' else 2


if __name__=='__main__':raise SystemExit(main())
