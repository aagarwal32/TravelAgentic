# SIGAIDA Agentic Travel Planner Project

## Project Details

TravelAgentic is an AI-powered travel planner that helps budget-conscious college students find the cheapest way to get where they're going. A user enters a trip, such as Chicago to New York, and the agent asks follow-up questions about budget, travel mode, rental cars, and activities. It then returns real, bookable flights, hotels, and rental cars from the Duffel API, along with charts showing which dates and months tend to be cheaper based on historical data. Behind the scenes, a LangGraph system coordinates specialized agents for historical price trends, live pricing, hotel preference matching, and a final evaluator that combines their results. Every price shown comes from live API data or the team's own historical database, never from the language model's guesswork.

### Members

| Name |
|------|
| Arjun Agarwal|
| Saatvik Palli |
| Inigo Serrano     |
| Wilson Zhu      |
| Gia Bao Ta     |
| Ethan Guo     |
| Duc Vo     |
| Ethan Lin     |

### Datasets

- DOT Consumer Airfare Report Table 6 (Departure, Arrival, Dates, Quarter (Q1, Q2, Q3, Q4), avg_price, lowest_price)
- Kaggle Flight Prices (supplemental for V2)
- Dotlas for Hotels

### ML Model
- This model will classify user queries on whether they are related to travel or not to avoid unnecessary LLM API costs.
- Inputs: text
- Outputs: True/False

## Getting Started

This guide walks you from a fresh machine to a running project. Commands are for macOS/Linux; Windows equivalents are noted where they differ.

### 1. Install Git

macOS: run `git --version` (it offers to install if missing). Windows: install from [git-scm.com](https://git-scm.com/downloads).

### 2. Set up Git and GitHub access

Tell Git who you are (use the email on your GitHub account):

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Create an SSH key and add it to GitHub (skip if you already have one in `~/.ssh/`):

```bash
ssh-keygen -t ed25519 -C "you@example.com"   # press Enter to accept the defaults
cat ~/.ssh/id_ed25519.pub                     # copy the output
```

Paste the output at **GitHub → Settings → SSH and GPG keys → New SSH key**, then confirm it works:

```bash
ssh -T git@github.com   # should print "Hi <username>! You've successfully authenticated..."
```

> You also need to accept the collaborator invite for this repo (check your email or GitHub notifications).

### 3. Clone the repo

```bash
git clone git@github.com:aagarwal32/TravelAgentic.git
cd TravelAgentic
```

Then follow the [Backend](#backend) and/or [Frontend](#frontend) setup below.

## Backend

All commands in this section run from `backend/`:

```bash
cd backend
```

### 1. Install Python

Python 3.13+ — download from [python.org](https://www.python.org/downloads/) or, on macOS, `brew install python@3.13`. Check with `python3 --version`.

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Run `source .venv/bin/activate` again in every new terminal. In VS Code, select the interpreter via **Cmd/Ctrl+Shift+P → Python: Select Interpreter → Enter interpreter path** → `backend/.venv/bin/python`.

### 3. Create your `.env` file

`.env` holds secrets and is not committed, so each person makes their own in `backend/`:

```env
SECRET_KEY=<generate one, see below>
ACCESS_TOKEN_EXPIRE_MINUTES=30
DEBUG=True
API_PREFIX=/api
DATABASE_URL=sqlite:///./database.db
ALLOWED_ORIGINS=http://localhost:3000
```

Generate a `SECRET_KEY` with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Ask a teammate for any shared values (e.g. API keys) — never commit them.

### 4. Download the datasets

Datasets are too large for Git, so they're fetched by a script into `backend/datasets/`:

```bash
python scripts/download_datasets.py          # all datasets
python scripts/download_datasets.py --list   # see what's available
```

The local database is also not committed; everyone builds their own from the datasets.

### 5. Run the server

```bash
python main.py
```

Open http://localhost:8000/docs to see the API.

### After pulling changes

```bash
pip install -r requirements.txt       # if requirements.txt changed
python scripts/download_datasets.py   # if new datasets were added
```

## Frontend

_Coming soon._

## Day-to-day Git workflow

```bash
git pull                              # get the latest changes
git checkout -b my-feature            # work on a branch
git add <files>
git commit -m "Describe your change"
git push -u origin my-feature         # then open a Pull Request on GitHub
```
