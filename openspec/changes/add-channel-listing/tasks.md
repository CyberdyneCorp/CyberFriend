## 1. The listing

- [x] 1.1 Intersect indexed scope with the asker's readable channels
- [x] 1.2 Resolve readable channels from live platform state per request
- [x] 1.3 Read scope live, so a change needs no redeploy
- [x] 1.4 Test: a channel the asker cannot read is absent
- [x] 1.5 Test: the reply discloses nothing about what was filtered out
- [x] 1.6 Test: an unresolvable asker gets an empty listing

## 2. The command

- [x] 2.1 A command that lists them, answered privately
- [x] 2.2 Say there are none to show, distinctly from failing
- [x] 2.3 List it in the capability reply, in both languages
- [x] 2.4 Test: the reply is ephemeral

## 3. Wiring and documentation

- [x] 3.1 Build it in `composition.py` and assert the call chain
- [x] 3.2 README and docs

## 4. Live

- [ ] 4.1 Deploy
- [ ] 4.2 Confirm it lists the indexed channels in Discord
