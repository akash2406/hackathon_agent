# Demo script (5 minutes)

Goal: show a judge that CRIP answers real FinOps questions **and** that every number is provably
real. Use a subscription with some history (a few weeks of spend) and at least one Advisor
recommendation or idle disk.

## Before you start

- Sign in once in advance (consent prompts out of the way).
- Open the app on one screen and the Azure portal (Cost Management → Cost analysis) on another.
- If the subscription is quiet, create an unattached disk and a public IP the day before, so
  Optimizer finds idle resources with a real cost.

## The flow

1. **The pitch (20 s).** "Cloud bills are opaque. CRIP is a FinOps copilot: ask in plain English, get
   an answer computed from live Azure data with *your own* permissions, and the proof attached."

2. **The headline question (90 s).** Click *Give me a cost overview: what I've spent, what month-end
   will be, and where I can save.*
   - Point at **consulted: CostPulse · spend, Optimizer · savings**: one question, several
     specialist agents, chosen by the Orchestrator itself.
   - Show the **projected month-end (Azure forecast)** tile and the actual-vs-forecast chart.
   - Show **estimated annual savings (Azure Advisor)** and the idle resources with their real
     month-to-date cost.

3. **The "why" question (60 s).** *Why did my costs change in the last 30 days, and what can I do
   about it?* Show the daily chart with the **▲ spike** day, the typical-day line, and the driver
   service. Hover a column for the tooltip.

4. **The proof (60 s). This is what wins.** Open the *Grounding proof* under any card:
   - **Query used**: the exact Azure API call. Paste the date range into Cost analysis; the
     numbers match.
   - **Data as of**: the latest billing day, not "now". Cost data lags 8-24h and CRIP says so.
   - **request id**: Azure's own id for that call.
   - "with your own permissions": sign in as a user **without** access to the subscription and ask
     again. CRIP says *"Azure denied access … you need Cost Management Reader"* and shows no numbers.
     No service account is reading data behind the scenes.

5. **Governance (30 s).** *How many of my resources are missing an 'owner' tag?* Inventory agent,
   coverage meter, worst resource groups.

6. **Close (20 s).** "Four agents in Azure AI Foundry, one container on App Service, read-only, OBO
   end to end. Adding an agent is three files: the Orchestrator picks it up on its own."

## If something goes wrong live

- An answer says *retrieval failed*: that *is* the feature (honest failure). Read out the reason.
- Slow first answer: the first Foundry run after a restart warms up. Ask a short question first.
- Audit evidence for a skeptical judge: the `agent_invocations` query in the README.
