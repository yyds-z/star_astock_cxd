@echo off 
cd /d e:\星空系统\astock_ai 
D:\miniconda3\envs\astock\python.exe -m astock.cli limit-pool --years 1 > data\logs\limitpool_backfill.log 2>&1
