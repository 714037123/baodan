# 补丁放在文件第一行
import importlib.metadata
_ori_version = importlib.metadata.version
def mock_version(pkg_name):
    if pkg_name == "streamlit":
        return "1.35.0"
    return _ori_version(pkg_name)
importlib.metadata.version = mock_version

# 下面再写你原来的导入代码
import streamlit as st
import sys
import os
import streamlit.web.cli as stcli

def run_streamlit():
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    app_path = os.path.join(base_dir, "script1.py")
    sys.argv = [
        "streamlit",
        "run",
        app_path,
        "--server.enableXsrfProtection=false",
        "--global.developmentMode=false",
        "--server.port=8501",
        "--browser.gatherUsageStats=false",
        "--server.headless=false"
    ]
    stcli.main()

if __name__ == "__main__":
    run_streamlit()