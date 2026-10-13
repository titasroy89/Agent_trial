# Daily job search agent

Searches Apple, Google, Microsoft, AbbVie, BCG, Prolaio and Netflix for Data Scientist /
Applied Scientist roles in Chicago or US remote paying over $130k, and emails you a digest.

## How it works
1. **Search**: for each company, Claude (with web search) finds open postings and returns JSON.
2. **Filter**: plain Python checks title, location (Chicago/remote) and pay (top of range > $130k).
   Postings with no pay listed go in a separate section.
3. **Dedup**: `seen_jobs.json` remembers what was already emailed; new ones are flagged NEW.
4. **Email**: sent through Gmail SMTP. Raw search results are saved in `runs/` for debugging.

Edit `COMPANIES`, `ROLE_TITLES`, `SKILLS` and `MIN_SALARY` at the top of `job_agent.py`.

## Setup
```bash
cd job_agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill it in
```
- **ANTHROPIC_API_KEY**: from console.anthropic.com.
- **GMAIL_APP_PASSWORD**: Google Account > Security > 2-Step Verification > App passwords.
  (Needs 2-Step Verification on. Your normal password won't work.)

## Run
```bash
python job_agent.py --dry-run   # prints the digest, sends nothing
python job_agent.py             # sends the email
```

## Schedule it every morning (6:58am)
macOS/Linux, `crontab -e`:
```
58 6 * * * cd /path/to/job_agent && .venv/bin/python job_agent.py >> agent.log 2>&1
```
The computer has to be on and awake at that time. To run it without your laptop,
use a GitHub Actions workflow with a `schedule:` trigger and store the three secrets there.

## Cost and limits
Each run makes 7 API calls with up to 8 web searches each. Web search finds most public
postings but can miss some or surface stale ones, so the email links to the source for you to confirm.
