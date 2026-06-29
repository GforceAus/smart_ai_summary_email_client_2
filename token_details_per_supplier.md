# Token Analysis per Supplier - Debug Results

Generated from `just debug_large` output on 2026-06-10

## Executive Summary

**Token Thresholds:**
- ✅ **Safe zone:** < 1500 tokens (fast generation)
- ⚠️  **Caution zone:** 1500-1700 tokens (may timeout)
- ❌ **Danger zone:** > 1700 tokens (likely timeout)

**Performance Results:**
- **NULON**: ✅ 1364 tokens → SUCCESS (238s generation)
- **OSRAM**: ❌ 1589 tokens → TIMEOUT (240s+)
- **KINCROME**: ❌ 1799 tokens → LIKELY TIMEOUT

## Detailed Analysis

### OSRAM (❌ TIMEOUT - 1589 tokens)
```
Tasks: 775 total, 570 done (73.5% completion)
Exception Data: 60 raw rows → 4 grouped rows
Stores: 226 visited, 43 with issues
Major Issue: "01-06-26 GIMBLE DOWNLIGHTS" - 43 stores "NO NOT RANGED"
```
**Problem:** Large dataset with widespread stock issues
**Status:** Confirmed timeout at 240s

### KINCROME (❌ HIGH RISK - 1799 tokens)
```
Tasks: 502 total, 496 done (98.8% completion)
Exception Data: 60 raw rows → 15 grouped rows (MAX_AGGREGATED_ROWS limit)
Stores: 73 visited, 63 with issues
Issues: High issue density despite good completion rate
```
**Problem:** Hits MAX_AGGREGATED_ROWS limit, complex issue patterns
**Status:** Not tested, but likely timeout based on token count

### NULON (✅ SUCCESS - 1364 tokens)
```
Tasks: 45 total, 44 done (97.8% completion)
Exception Data: 45 raw rows → 2 grouped rows
Stores: 22 visited, 0 with issues
Main Tasks: Recurring photo tasks
```
**Problem:** None - clean, simple dataset
**Status:** Confirmed success (238s generation)

## Technical Details

### System Constraints
- **Context Window:** 4096 tokens (hard limit)
- **Timeout:** 240 seconds
- **MAX_AGGREGATED_ROWS:** 15 (reduces 60 raw → 15 max grouped)
- **LLM Model:** Qwen2.5 7B Q4_K_M (CPU inference)

### Token Breakdown Structure
```
System Prompt: ~1282 chars (constant)
Few-shot Examples: Variable (1-2 examples)
Data Payload: Variable based on:
  - Number of exception rows
  - Store names and counts  
  - Rep comments
  - Task complexity
```

## Recommendations

### Immediate Actions
1. **Avoid OSRAM and KINCROME** in batch runs until optimized
2. **Use NULON, TIMEPET** as reliable test cases
3. **Run token_analysis** before full deployment

### Optimization Strategies
1. **Reduce MAX_AGGREGATED_ROWS** from 15 → 10 for high-volume suppliers
2. **Implement store grouping** (e.g., "43 stores across QLD/NSW" vs listing all)
3. **Truncate rep comments** to essential information only
4. **Consider supplier-specific timeouts** (300s for complex suppliers)

### Testing Protocol
```bash
# Before any supplier batch run:
just token_analysis          # Check all token counts
just quick_success           # Verify system health
just test_problematic       # Handle known issues
```

## Supplier Classification

### ✅ Low Risk (< 1500 tokens)
- NULON (1364)
- TIMEPET (476 - from previous test)

### ⚠️ Medium Risk (1500-1700 tokens)
- OSRAM (1589) - **Confirmed timeout**

### ❌ High Risk (> 1700 tokens)  
- KINCROME (1799) - **Untested, likely timeout**

## Production Readiness

**Ready for automation:**
- Weekly: NULON
- Fortnightly: TIMEPET

**Requires optimization:**
- Weekly: OSRAM, KINCROME (likely others in weekly group)
- Need full token_analysis on all high-frequency suppliers

**Next Steps:**
1. Run `just token_analysis` for comprehensive assessment
2. Implement token reduction strategies for problematic suppliers
3. Create supplier-specific configurations in email generator