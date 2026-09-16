## Skill routing

The rules below name skills from [gstack](https://github.com/garrytan/gstack), an
optional open-source skill pack. This repository does not require it and does not
install it. If gstack is not installed, none of these skills appear in the available
skills listing — skip this section entirely rather than calling a skill by name.

When the user's request matches an available skill, invoke it via the Skill tool. When in
doubt, invoke the skill. The names below are written in the slash form the user types; the
Skill tool takes the bare name from the available-skills listing, without the leading slash.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
