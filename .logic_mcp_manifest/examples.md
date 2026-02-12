# Logic MCP Examples (v5)

Use these as compact patterns; adapt ids and symbols to your project.

## 1) Spec-time invariant
Goal: capture a discovered dependency.

`logic_set_rule`
```json
{"id":"retry_limit_non_negative","lang":"pyexpr","rule":"retry_limit >= 0"}
```

## 2) Thought experiment: relax a rule
Goal: see if a weaker design still satisfies expectations.

`logic_check`
```json
{
  "hypothesis": {
    "patch": {
      "set_rules": {
        "timeout_upper_bound": {"lang":"pyexpr","rule":"timeout_ms <= 15000"}
      },
      "remove_rules": []
    }
  },
  "detail_level": "compact"
}
```

Read: `breaks`, `delta`, `expectation_failures`.

## 3) Search for less-constrained valid values
Goal: leave values symbolic and let solver pick witness values.

`logic_check`
```json
{
  "hypothesis": {
    "facts": {"batch_size":"?b:Int","timeout_ms":"?t:Int"},
    "patch": {"set_rules": {}, "remove_rules": []}
  },
  "detail_level": "full"
}
```

Read: `baseline.model` and `candidate.model`.

## 4) Detect missing constraint with expectation
Goal: ensure one rule structurally implies another.

`logic_set_expectation`
```json
{
  "id":"exp_no_negative_balance",
  "kind":"entails",
  "a_ref":"ledger_rules",
  "b_ref":"balance_non_negative"
}
```

Then run `logic_check` and inspect expectation statuses at `detail_level` `more` or `full`.

## 5) Keep context links close to logic
Goal: connect source concepts and code artifacts to rule ids.

`logic_context_patch`
```json
{
  "ops": [
    {
      "op":"set_concept",
      "id":"c_retry_policy",
      "set":{
        "concept":"Retry Policy",
        "meaning":"Caps retries and enforces backoff",
        "primary_symbols":["retry_limit","backoff_ms"],
        "related_rule_ids":["retry_limit_non_negative"],
        "related_expectation_ids":[],
        "related_code_binding_ids":["cb_retry_impl"]
      }
    },
    {
      "op":"set_code_binding",
      "id":"cb_retry_impl",
      "set":{
        "path":"src/retry/policy.py",
        "related_rule_ids":["retry_limit_non_negative"],
        "related_expectation_ids":[],
        "related_concept_ids":["c_retry_policy"]
      }
    }
  ]
}
```
