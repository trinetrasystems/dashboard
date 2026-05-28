# Quick Start Guide

This document contains the commands required to run the Trinetra Systems Monitor.

## 1. First Time Setup & Run
If you are deploying this on a new server or running it for the very first time, you must install the Python dependencies into a virtual environment.

Run these commands in your terminal:

```bash
# Go to the project directory
cd /home/yash/dashboard

# Create a virtual environment named "venv"
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

# Install the necessary backend dependencies
pip install -r backend/requirements.txt

# Start the dashboard server
python backend/app.py
```

---

## 2. Subsequent Runs
If you have already run the setup commands above previously, you do not need to install the requirements again. Simply activate the virtual environment and start the server.

Run these commands in your terminal:

```bash
# Go to the project directory
cd /home/yash/dashboard

# Activate the virtual environment
source venv/bin/activate

# Start the dashboard server
python backend/app.py
```

*The server will start at http://0.0.0.0:8000. You can stop it at any time by pressing `Ctrl + C`.*
