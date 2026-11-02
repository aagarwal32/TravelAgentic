# SIGAIDA Agentic Travel Planner Project

## Project Details

TravelAgentic is an AI-powered travel planner that helps budget-conscious college students find the cheapest way to get where they're going. A user describes a trip in their own words, such as "Chicago to New York in March for under $500", and the app fills in any missing details with quick question cards: dates, budget, number of travelers, whether to fly or drive, and optional preferences like layovers, activities, and hotel wishes. It then shows real flight prices from the Duffel API (skipped for road trips) and real hotel prices from LiteAPI, along with which seasons tend to be cheaper for the route based on historical airfare data. TravelAgentic finds and compares options; it doesn't book them.

Behind the scenes, a LangGraph system coordinates specialized agents for trip intake, historical price trends, live pricing, hotel matching, and a final evaluator that combines their results. Every price shown comes from live API data or the team's own historical database, never from the language model's guesswork.

### Members

Arjun Agarwal · Saatvik Palli · Inigo Serrano · Wilson Zhu · Gia Bao Ta · Ethan Guo · Duc Vo · Ethan Lin · Alex Lien

### Data sources

| Source | Used for |
|---|---|
| [Duffel API](https://duffel.com/) | Live flight prices |
| [LiteAPI](https://www.liteapi.travel/) | Hotels and live hotel prices near the destination ([docs/hotels.md](docs/hotels.md)) |
| [DOT Consumer Airfare Report, Table 6](https://data.transportation.gov/d/yj5y-b2ir) | Historical average fares by city pair and quarter, 2022 onward ([docs/data.md](docs/data.md)) |
| Kaggle Flight Prices | Possible supplement for V2 |

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
ALLOWED_ORIGINS=http://localhost:3000,http://localhost:5173
# optional, for hotel search (see docs/hotels.md):
LITEAPI_API_KEY=<your LiteAPI sandbox key>
# optional, for the trip-intake agent (see docs/agent.md):
ANTHROPIC_API_KEY=<your Claude API key>
LLM_MODEL=claude-haiku-5-5
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

The local database is also not committed; everyone builds their own from the datasets. See [docs/data.md](docs/data.md) for loading each dataset into the database.

### 5. Run the server

From `backend/`, with the virtual environment active:

```bash
fastapi dev main.py
```

Open http://localhost:8000/docs to see the API. The server reloads when you save a file, and creates any missing database tables on startup.

### 6. Run the tests

From `backend/`, with the virtual environment active (same command on Mac, Linux, and Windows):

```bash
python -m unittest discover -s tests -v
```

The tests never call real APIs or touch `database.db`, and all of them should pass before you open a pull request.

### After pulling changes

```bash
pip install -r requirements.txt       # if requirements.txt changed
python scripts/download_datasets.py   # if new datasets were added
```

## Frontend

_Coming soon._

## Docs

How to use each part of the project, beyond setup:

| Doc | Covers |
|---|---|
| [docs/agent.md](docs/agent.md) | The trip-intake agent and its terminal demo |
| [docs/data.md](docs/data.md) | Downloading datasets and loading DOT Table 6 |
| [docs/hotels.md](docs/hotels.md) | LiteAPI setup and the hotel search functions |

## Day-to-Day Git Workflow

### 1. Pick an issue and create its branch on GitHub
- Go to the **Issues** tab and pick an issue assigned to you (filter with `assignee:@me`).
- In the issue's right sidebar, under **Development**, click **Create a branch**.
- Keep GitHub's suggested name. It starts with the issue number followed by the issue title
  (e.g. `<issue-number>-<short-description>`).

### 2. Switch to the branch, activate the environment, and update
```bash
git fetch origin                     # let your computer know the new branch exists
git checkout <branch-name>           # switch to the branch GitHub created

source .venv/bin/activate            # Mac/Linux
# .venv\Scripts\activate             # Windows

git pull                             # latest changes on your branch
git pull origin main                 # bring in work already merged into main
pip install -r requirements.txt      # install any packages teammates added
```

### 3. Work on the feature and commit as you go
```bash
git add <files>
git commit -m "<short description of the change>"
```
Commit after each working step, not only at the end.
Run `git pull origin main` every day or two to stay up to date and avoid merge conflicts.

### 4. Update requirements (only if you installed new packages), then push
```bash
pip freeze > requirements.txt
git diff requirements.txt            # check that only packages your feature needs were added
git add requirements.txt
git commit -m "Update requirements"
git push -u origin <branch-name>
```

### 5. Open a Pull Request on GitHub
- Write `Closes #<issue-number>` in the PR description so the issue closes automatically when merged.
- Request a review from a teammate. Never push directly to `main`.
- Put what you changed and how you checked it in the PR description. If your feature needs instructions (commands, settings), add or update a page in `docs/` and link it from [Docs](#docs). Only change this README when setup changes.

### Command reference
| Command | What it does |
|---|---|
| `git fetch origin` | Downloads info about new branches and commits from GitHub; does **not** change your files |
| `git pull` | Downloads and applies new commits on **your current branch** |
| `git pull origin main` | Downloads the latest `main` and merges it **into your current branch** (you stay on your branch) |
