# Frontend regression / handoff

Run (Node 18+; no dependencies/network/server required):

```sh
node --test tests/frontend/*.test.cjs
```

`navigation.test.cjs` executes both real inline scripts from `app/static/index.html` in Node `vm`. A minimal DOM/fetch/timer fixture supplies browser boundaries; navigation, query construction, rendering, request races, paging, delete confirmations, save persistence warnings and boot functions are real. CSS layout, actual accessibility-tree semantics and focus geometry are **not** proven by this fixture.

## Actual results

- Latest execution: **22 tests, 22 passed, 0 failed**; full genuine TAP output in `latest-test-output.txt`.
- Syntax validation: **2 inline scripts + 46 HTML event handlers compiled successfully**.
- `git diff --check -- app/static/index.html tests/frontend`: exit 0.
- RED was observed before each changed behavior slice (navigation; aggregate/paging/loading; debounce/mobile/boot; deletion/trash; persistence/focus; collision-free query delete and structured errors). Final extra negative/markup/startup tests audit the implemented paths.
- This worker did not launch a browser, contact production, restart a service, change backend files or commit.

## API integration

- Overview / sidebar: `/api/v1/projects` aggregate, not an arbitrary initial document page.
- `null` means all projects; `''` means unassigned (`unassigned=true`). Real project names, including `__none__`, `未归项目`, `（未归项目）`, `.`, `..`, slash/quote names, stay literal.
- Project bulk deletion uses the new **`DELETE /api/v1/projects?project=...`** or `?unassigned=true` query endpoint, avoiding the legacy sentinel/path ambiguity. Confirmation freshly fetches a full unfiltered project total; label explicitly says all libraries / hidden-by-filter documents.
- Trash purge freshly fetches `total`, requires confirm + typed 清空, then sends `POST /api/v1/trash/empty?expected_count=N`. A 409 displays `detail.message`, refreshes the trash and does not automatically repeat deletion.
- Document save warning accepts `persistenceStatus` or `persistence_status`; failed Git state remains visible in `#save-status`, with a warning toast rather than a false commit claim.

## Parent browser checks (isolated test server only)

1. Normal cached-token page startup (no manual `boot()` injection): all-documents first/selected, aggregate total over 200, folder grid present.
2. Enter real / unassigned / placeholder-named projects; exactly one `.navitem.on`; hit “全部文档” from project/search/edit; project, q, type and library filters reset.
3. 320 / 375 / 760 / 1280 widths, both themes: no document/toolbars horizontal overflow, long title/project truncation or wrapping, all select controls usable. No tree view.
4. Mobile `#nav-toggle`: drawer opens, backdrop / Escape / choosing entry closes, navigation scrolls to all entries, Tab loops, focus returns appropriately. Check desktop → mobile resize too.
5. Folder/document Enter + Space navigation; delete buttons visible on coarse-pointer/touch and keyboard focus. Deleting must not also enter the folder. Dangerous fixtures only on test server; cancel is safe.
6. Project list `#lmore` and trash `#tmore`: 50 then next page, accurate full total, final button hidden. Inject delayed/error responses; stale requests never replace current list, retry works, failed next page preserves existing cards.
7. Rapid title input / Chinese IME; one debounced current request, Enter immediate, changing view cancels timer. Loading / empty / error are distinct.
8. Project deletion filtered-grid count vs actual entire project count; trash total > 50; cancellation and count-changing 409. Never run destructive cases against production data.
9. Auth API 503 leaves token/retry available; 401 clears it. Save with Git failure displays persistent warning and saved document remains editable; subsequent successful save clears warning.
