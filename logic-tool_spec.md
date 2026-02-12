> Deprecated. This legacy draft is superseded by `context-feature-spec.md` and does not match the implemented v5 API.

````markdown
# Formal specification: Logic tool family for agentic what-if checking

## 1. Purpose and goals

This tool family supports incremental specification capture and low-effort what-if validation during implementation.

It is designed for two phases:

- Spec clarification: the agent may add precise constraints, including direct Z3 or SMT-LIB2 fragments
- Implementation: the agent can cheaply ask “would it break something if I do this” by supplying a small hypothesis overlay and a temporary patch, without rewriting the model

Key goals:

- Persist constraints across the whole chat session and across agents working in the same project namespace
- Make what-if checks low friction
- Always check against the full active rule set, no targeted checks
- Provide actionable diagnostics: unsat cores, counterexamples and change impact
- Support mixing rule styles: restricted python-like expressions and solver-native fragments

Non-goals:

- Proving program correctness end to end
- Supporting arbitrary Python execution
- Inferring user intent without explicit expectations or properties

## 2. Terminology

- Store: persistent state managed by the tool for a given namespace
- Rule: a persistent constraint that must hold
- Bundle: a persistent solver fragment, typically declarations and helper definitions
- Expectation: a persistent meta-property, typically an entailment or equivalence relationship between rules or expressions
- Hypothesis: a request-local overlay of facts and a request-local patch to the store for exploration
- Patch: request-local modifications to the active rule set, never persisted automatically
- Baseline: evaluation of the store with the hypothesis facts overlay but without the patch
- Candidate: evaluation of the store with the hypothesis facts overlay and the patch applied

## 3. Tool namespace and persistence model

### 3.1 Namespace resolution
The environment supplies namespace identifiers, for example project name and chat session. The tool must not require the caller to include session ids, agent ids or roles in payloads.

Persistence is scoped by `(namespace_id)`, where the environment defines how `namespace_id` is derived.

### 3.2 Store contents
The store contains:

- Bundles
  - Declarations, helper functions, macros and shared predicates
- Rules
  - Named constraints that must hold
- Expectations
  - Named meta-properties to detect omissions and unintended freedom
- Defaults
  - Optional persistent facts, domains and preferred types

All store items are versioned. Replacing an item creates a new version and deactivates the prior version.

### 3.3 Active set
At any time the active model is:

- All enabled bundles in insertion order
- All enabled rules in insertion order
- All enabled expectations in insertion order
- All defaults

## 4. Supported rule languages

The tool supports multiple input languages. The caller declares the language per item.

### 4.1 `pyexpr` (restricted python-like expression)
A single expression that evaluates to boolean.

Allowed constructs:

- Literals: integers, reals, booleans
- Variables: identifiers
- Operators: `+ - * / %`, comparisons, `and or not`, parentheses
- Functions: `abs`, `min`, `max`
- Conditional expression: `a if cond else b` is allowed only if it compiles into solver form

Not allowed:

- Statements of any kind
- Imports
- Attribute access, indexing, slicing
- Comprehensions, generators, lambdas
- `eval`, `exec`
- Access to IO, filesystem, network, time

Compilation:
- `pyexpr` is parsed by the tool and compiled to solver constraints
- If a `pyexpr` cannot be compiled, the tool must reject it with a clear error

### 4.2 `smt2` (SMT-LIB2 fragments)
A list of SMT-LIB2 commands, typically declarations, `define-fun` and `assert`.

Requirements:

- Any asserted constraints intended to appear in unsat cores must be named using `:named`
- The tool must wrap assertions where needed so it can report item ids reliably

### 4.3 `z3py` (optional)
If supported, the tool may accept a string of Z3 Python that builds expressions only, not statements. This is optional. Implementations may omit it and rely on `smt2` instead.

## 5. Symbol handling and types

### 5.1 Symbol table
The store maintains a symbol table for each namespace:

- name
- sort: `Int`, `Real`, `Bool` and optionally `Enum`
- optional unit suffix convention, for example `_ms`

### 5.2 Type inference
If a symbol is referenced and not declared:

- If using `pyexpr`, infer sort from usage where possible
- If cannot infer, default to `Int`

If using `smt2`, declarations inside bundles take precedence.

### 5.3 Facts and symbolic values in hypotheses
A hypothesis can overlay facts.

Fact values may be:

- Concrete: `0`, `2`, `true`, `false`, `1.5`
- Symbolic placeholder: a string starting with `?`, for example `"?t2"`

Symbolic placeholders mean “leave unconstrained and allow the solver to choose a value”.

Optional typed placeholders are allowed:
- `"?t2:Int"`, `"?x:Real"`, `"?flag:Bool"`

If type is omitted, use the symbol table sort if known, else use `Int`.

## 6. Tool API

All tools accept JSON requests and return JSON responses.

Common response fields:

- `ok`: boolean
- On success, `result`: object
- On failure, `error`: object with `code`, `message` and optional `details`

### 6.1 `logic_rule_add`
Adds a persistent rule.

Request schema:
```json
{
  "tool": "logic_rule_add",
  "version": "1.0",
  "id": "string",
  "lang": "pyexpr | smt2",
  "rule": "string | string[]",
  "enabled": true
}
````

Notes:

* If `lang` is `pyexpr`, `rule` is a single string expression
* If `lang` is `smt2`, `rule` is a list of SMT-LIB2 commands, or a single string that the tool splits by line

Response:

```json
{
  "ok": true,
  "result": {
    "id": "string",
    "version": 1
  }
}
```

### 6.2 `logic_rule_replace`

Replaces an existing rule by id.

Request:

```json
{
  "tool": "logic_rule_replace",
  "version": "1.0",
  "id": "string",
  "lang": "pyexpr | smt2",
  "rule": "string | string[]",
  "enabled": true
}
```

Response includes new version.

### 6.3 `logic_rule_delete`

Disables a rule by id.

Request:

```json
{
  "tool": "logic_rule_delete",
  "version": "1.0",
  "id": "string"
}
```

### 6.4 `logic_list`

Lists active and optionally inactive items.

Request:

```json
{
  "tool": "logic_list",
  "version": "1.0",
  "include_disabled": false,
  "include_bundles": true,
  "include_rules": true,
  "include_expectations": false
}
```

Response:

```json
{
  "ok": true,
  "result": {
    "bundles": [],
    "rules": [],
    "expectations": []
  }
}
```

Each item entry must include:

* `id`, `version`, `enabled`, `lang`, `content_summary`

### 6.5 `logic_bundle_add`

Adds a persistent bundle, typically declarations and helper predicates.

Request:

```json
{
  "tool": "logic_bundle_add",
  "version": "1.0",
  "id": "string",
  "lang": "smt2",
  "bundle": "string | string[]",
  "enabled": true
}
```

Bundles are applied before rules.

### 6.6 `logic_expect_add` (optional but recommended)

Adds an expectation that can detect omissions.

Two expectation kinds are required:

* `entails`: rules imply a target expression
* `equivalent`: rule or expression A is equivalent to rule or expression B

Request:

```json
{
  "tool": "logic_expect_add",
  "version": "1.0",
  "id": "string",
  "expect": {
    "kind": "entails | equivalent",
    "a": { "ref": "rule_id" } ,
    "b": { "expr": "pyexpr expression string" }
  }
}
```

For `entails`, interpret as `a entails b`.
For `equivalent`, interpret as `a <-> b`.

The tool compiles `expr` using `pyexpr` rules. Implementations may allow `b.ref` as an alternative to `b.expr`.

Expectation checking is described in section 7.5.

### 6.7 `logic_check`

Runs a what-if check.

Request:

```json
{
  "tool": "logic_check",
  "version": "1.0",
  "hypothesis": {
    "facts": { "name": "value" },
    "patch": {
      "add": { "id": { "lang": "pyexpr | smt2", "rule": "string | string[]" } },
      "replace": { "id": { "lang": "pyexpr | smt2", "rule": "string | string[]" } },
      "delete": [ "id" ]
    }
  },
  "options": {
    "analyse_influence": true,
    "return_models": true,
    "return_unsat_core": true,
    "check_expectations": true
  }
}
```

Defaults:

* If `hypothesis` is omitted, it is treated as empty
* If `patch` is omitted, it is treated as empty
* `options.analyse_influence` defaults to true
* `options.return_models` defaults to true
* `options.return_unsat_core` defaults to true
* `options.check_expectations` defaults to true if expectations exist, else false

Targeted checks are not supported. The tool always checks against the full active model.

## 7. Semantics and algorithms

### 7.1 Baseline and candidate evaluation

Given a `logic_check` request:

* Baseline = active store, apply `hypothesis.facts` overlay, ignore `hypothesis.patch`
* Candidate = baseline plus patch applied in this order:

  * delete
  * replace
  * add

No patch content is persisted.

### 7.2 Overlay semantics for facts

`hypothesis.facts` overlays any defaults and solver-chosen values.

If a fact provides a concrete value, constrain the corresponding symbol to that value.
If a fact provides a symbolic placeholder, ensure the symbol exists but do not constrain it.

### 7.3 Patch semantics

* `delete`: disables the referenced rule for candidate evaluation only
* `replace`: candidate uses the replacement content instead of the stored rule content
* `add`: adds a new temporary rule for candidate evaluation only

Temporary rules participate in unsat cores and influence analysis.

### 7.4 Status values

Each evaluation returns a status:

* `sat`: satisfiable
* `unsat`: unsatisfiable
* `unknown`: solver returned unknown or timed out

Baseline and candidate each have their own status.

`breaks` is computed as:

* `breaks = (baseline.status == "sat") and (candidate.status != "sat")`

### 7.5 Expectations

Expectation checking is done after baseline and candidate checks.

For an `entails` expectation `A entails B`:

* Compute satisfiable of: `A_context and not(B)`
* If satisfiable, expectation fails and the tool returns a counterexample model

`A_context` means:

* All active rules, plus the rule referenced by `A` if it is not already part of the rule set
* Baseline or candidate context depending on which check is being run

For `equivalent` expectation `A <-> B`:

* Check `A entails B` and `B entails A`
* If either direction fails, expectation fails

Expectation failures are a primary mechanism to indicate that the agent may have missed a constraint, or that the expectation is wrong.

### 7.6 Unsat core

If status is `unsat` and `return_unsat_core` is true:

* Return an unsat core containing rule ids and bundle ids where applicable
* For `pyexpr` rules, the tool must internally name each assertion so it can be reported by rule id
* For `smt2` rules, the tool should prefer `:named` if present, else wrap assertions to ensure traceability

### 7.7 Model extraction

If status is `sat` and `return_models` is true:

* Return a witness model containing:

  * all symbols referenced by hypothesis facts
  * any symbols that were introduced by symbolic placeholders
  * optionally any symbols referenced by failed expectations

Model values should be returned in JSON-friendly form:

* integers as numbers
* reals as strings if needed to preserve exactness
* booleans as true or false

### 7.8 Influence analysis

Goal: report whether the patch changed anything, including sat to sat cases.

Definitions:

* `patch_influence` is true if any patched operation is influential
* For sat to unsat, influence is trivially true
* For unsat to sat, influence is trivially true
* For sat to sat, evaluate influence per added or replaced rule

Per-rule influence algorithm for added rule `r`:

* Run check: `baseline_constraints and not(r)`
* If `unsat`, then baseline entails `r` and the addition is non-influential
* If `sat`, then baseline does not entail `r` and the addition is influential

For deleted rule `d`:

* Run check: `candidate_constraints and not(d)`

  * Here `candidate_constraints` means baseline plus the deletion, without other patch operations unless the caller wants a combined analysis
* If `sat`, then deleting `d` enables a violation of `d` and is influential
* If `unsat`, then `d` was redundant under current context and deleting it is non-influential

For replace, treat as delete of old and add of new.

Influence analysis must be bounded:

* Use a solver call budget, for example max 20 additional checks
* If budget exceeded, return `influence: "unknown"` and include which checks were skipped

### 7.9 Result delta

The tool must report the practical impact:

* `newly_failed`: ids that are satisfied in baseline but fail in candidate, including expectations
* `no_longer_failed`: ids that fail in baseline but are satisfied in candidate
* `still_failed`: ids that fail in both
* `unknown`: ids where status is unknown

For `unsat`, failure is represented by the unsat core subset, not by individual rule evaluation. In this case:

* `newly_failed` should contain the unsat core ids that appear only in candidate
* `still_failed` should contain the core ids shared
* `no_longer_failed` should contain core ids that disappear

If unsat cores are not available, return `delta` with `unknown` populated.

## 8. Error handling

Errors must be explicit and machine actionable.

Error codes:

* `E_INVALID_REQUEST`: missing required fields, wrong types
* `E_UNKNOWN_ID`: referenced id does not exist
* `E_PARSE_ERROR`: cannot parse `pyexpr` or `smt2`
* `E_UNSUPPORTED`: unsupported construct
* `E_SOLVER_ERROR`: solver internal error
* `E_TIMEOUT`: solver timeout

Example error:

```json
{
  "ok": false,
  "error": {
    "code": "E_PARSE_ERROR",
    "message": "Invalid pyexpr: unexpected token '@' at position 14",
    "details": { "id": "sim_start_exact" }
  }
}
```

## 9. Performance requirements

* The implementation should use incremental solving with push and pop
* The base solver context built from bundles and persistent rules should be cached
* `logic_check` should reuse cached contexts and only add overlays and patch constraints per call
* Influence analysis must be bounded by a call budget and a time budget

## 10. Security and safety requirements

* `pyexpr` must never execute as Python code
* No filesystem, network, process execution or reflection
* SMT-LIB2 fragments are treated as declarations and assertions only, no solver state operations that escape the sandbox
* Resource limits must be enforced: timeouts, maximum AST size and maximum number of symbols

## 11. Minimal end-to-end example

### Step 1: Add a bundle with declarations

```json
{
  "tool": "logic_bundle_add",
  "version": "1.0",
  "id": "decl_times",
  "lang": "smt2",
  "bundle": [
    "(declare-const start_time_sim1_ms Int)",
    "(declare-const start_time_sim2_ms Int)"
  ]
}
```

### Step 2: Add a rule

```json
{
  "tool": "logic_rule_add",
  "version": "1.0",
  "id": "sim_start_exact",
  "lang": "pyexpr",
  "rule": "start_time_sim1_ms == start_time_sim2_ms"
}
```

### Step 3: What-if check with a candidate patch

```json
{
  "tool": "logic_check",
  "version": "1.0",
  "hypothesis": {
    "facts": {
      "start_time_sim1_ms": 0,
      "start_time_sim2_ms": "?t2"
    },
    "patch": {
      "add": {
        "sim1_first": { "lang": "pyexpr", "rule": "start_time_sim2_ms > start_time_sim1_ms" },
        "within_2ms": { "lang": "pyexpr", "rule": "(start_time_sim2_ms - start_time_sim1_ms) <= 2" }
      },
      "replace": {},
      "delete": []
    }
  }
}
```

Expected behaviour:

* Baseline is satisfiable with `t2 = 0`
* Candidate is unsatisfiable due to conflict between `sim_start_exact` and `sim1_first`
* Unsat core includes `sim_start_exact` and `sim1_first`
* Influence is true

```
::contentReference[oaicite:0]{index=0}
```
