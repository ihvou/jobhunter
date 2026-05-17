# Leadhunter Discovery

Goal: find high-signal lead sources for a user-provided ICP.

Good sources:

- public company/team pages
- founder directories and startup databases with public pages
- conference speaker lists
- GitHub organizations and contributors
- Product Hunt launches
- funding/news announcements
- hiring posts that reveal team ownership or buying intent
- public community directories
- compliant paid lead APIs when configured

Avoid:

- logged-in LinkedIn scraping
- hidden/private profile data
- personal email guessing
- automated outreach

Return candidates with:

- person or company
- role/title
- evidence URL
- why they match the ICP
- contact surface, if public
- risk level
- confidence

After the user approves candidates, store them with `leadhunter_save_leads`. After the user approves a repeatable source, store it with `leadhunter_add_lead_source`.
