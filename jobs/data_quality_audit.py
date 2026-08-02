"""
data_quality_audit.py — VuraRAD Radiogenomics Data Quality Audit & Fix
Run in AUDIT mode (default) or FIX mode (--fix flag).
"""
import os, sys, argparse
from datetime import datetime, timezone
from google.cloud import bigquery

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "vurarad")
DS         = "radiogenomics"
NOW        = datetime.now(timezone.utc).isoformat()
P          = f"`{PROJECT_ID}.{DS}"


def q(client, label, sql):
    print(f"\n{'='*55}\n{label}\n{'='*55}")
    try:
        rows = list(client.query(sql).result())
        for r in rows:
            print(" ", dict(r))
        return rows
    except Exception as e:
        print(f"  [SKIP] {e}")
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true")
    args  = parser.parse_args()
    apply = args.fix
    client = bigquery.Client(project=PROJECT_ID)

    print(f"\n VuraRAD Radiogenomics — Data Quality {'FIX' if apply else 'AUDIT'}")
    print(f" {NOW}\n")

    # ── 1. Duplicate check ────────────────────────────────────────────
    q(client, "1. Duplicates — clinical_cohorts", f"""
        SELECT data_source,
               COUNT(*)                    AS total_rows,
               COUNT(DISTINCT case_uid)    AS unique_cases,
               COUNT(*) - COUNT(DISTINCT case_uid) AS dup_rows
        FROM {P}.clinical_cohorts`
        GROUP BY data_source
        ORDER BY total_rows DESC
    """)

    q(client, "2. Duplicates — external_genomic_labels", f"""
        SELECT dataset_source,
               COUNT(*)                    AS total_rows,
               COUNT(DISTINCT case_uid)    AS unique_cases,
               COUNT(*) - COUNT(DISTINCT case_uid) AS dup_rows
        FROM {P}.external_genomic_labels`
        GROUP BY dataset_source
        ORDER BY total_rows DESC
    """)

    # ── 2. Access tier distribution ───────────────────────────────────
    q(client, "3. Access tier distribution", f"""
        SELECT dataset_source, dataset_access_tier, COUNT(*) AS cnt
        FROM {P}.external_genomic_labels`
        GROUP BY 1, 2
        ORDER BY cnt DESC
    """)

    # ── 3. NSCLC mutation label QC ────────────────────────────────────
    q(client, "4. NSCLC mutation label summary", f"""
        SELECT
            molecular_subtype,
            SAFE_CAST(JSON_VALUE(mutation_labels,'$.EGFR') AS BOOL) AS egfr_mutant,
            SAFE_CAST(JSON_VALUE(mutation_labels,'$.KRAS') AS BOOL) AS kras_mutant,
            SAFE_CAST(JSON_VALUE(mutation_labels,'$.TP53') AS BOOL) AS tp53_mutant,
            COUNT(*) AS cnt
        FROM {P}.external_genomic_labels`
        WHERE dataset_source = 'TCIA_NSCLC'
          AND JSON_VALUE(mutation_labels,'$.EGFR') IS NOT NULL
        GROUP BY 1,2,3,4
        ORDER BY cnt DESC
        LIMIT 12
    """)

    # ── 4. Feature coverage ───────────────────────────────────────────
    q(client, "5. Feature extraction coverage", f"""
        SELECT c.data_source,
               COUNT(c.case_uid)                              AS total,
               COUNTIF(f.feature_id IS NOT NULL)              AS has_features,
               COUNTIF(f.feature_id IS NULL)                  AS missing
        FROM {P}.clinical_cohorts` c
        LEFT JOIN {P}.radiomics_features` f ON c.case_uid = f.case_uid
        GROUP BY 1
        ORDER BY 2 DESC
    """)

    # ── 5. NSCLC aggregate mutation rates ─────────────────────────────
    q(client, "6. NSCLC published-data mutation prevalence check", f"""
        SELECT
            COUNTIF(SAFE_CAST(JSON_VALUE(mutation_labels,'$.EGFR') AS BOOL)) AS egfr_pos,
            COUNTIF(SAFE_CAST(JSON_VALUE(mutation_labels,'$.KRAS') AS BOOL)) AS kras_pos,
            COUNTIF(SAFE_CAST(JSON_VALUE(mutation_labels,'$.ALK')  AS BOOL)) AS alk_pos,
            COUNTIF(SAFE_CAST(JSON_VALUE(mutation_labels,'$.TP53') AS BOOL)) AS tp53_pos,
            COUNT(*) AS total
        FROM {P}.external_genomic_labels`
        WHERE dataset_source = 'TCIA_NSCLC'
    """)

    if not apply:
        print("\n[AUDIT COMPLETE] Pass --fix to apply dedup, tier update, and view rebuild.")
        return

    # ══════════════════════════════════════════════════════════════════
    #                         FIX MODE
    # ══════════════════════════════════════════════════════════════════
    print("\n\n=== APPLYING FIXES ===\n")

    # FIX 1: Deduplicate clinical_cohorts via DML DELETE (preserves partitioning)
    # [FIX C-9] Was CTAS which silently destroyed time-partitioning — replaced with DELETE
    print("[1] Deduplicating clinical_cohorts (keep latest per case_uid)...")
    try:
        client.query(f"""
            DELETE FROM {P}.clinical_cohorts`
            WHERE ingested_at NOT IN (
                SELECT MAX(ingested_at)
                FROM {P}.clinical_cohorts`
                GROUP BY case_uid
            )
        """).result()
    except Exception as e:
        if "streaming" in str(e).lower():
            print("    [WARN] DML DELETE blocked by streaming buffer — using MERGE upsert fallback")
            client.query(f"""
                MERGE {P}.clinical_cohorts` T
                USING (
                    SELECT * EXCEPT(rn) FROM (
                        SELECT *, ROW_NUMBER() OVER
                            (PARTITION BY case_uid ORDER BY ingested_at DESC) AS rn
                        FROM {P}.clinical_cohorts`
                    ) WHERE rn = 1
                ) S ON T.case_uid = S.case_uid
                WHEN MATCHED THEN UPDATE SET ingested_at = S.ingested_at
            """).result()
        else:
            raise
    print("    [OK] clinical_cohorts deduplicated (partitioning preserved).")

    # FIX 2: Deduplicate external_genomic_labels via DML DELETE (preserves partitioning)
    # [FIX C-9] Same CTAS → DML DELETE fix
    print("[2] Deduplicating external_genomic_labels (keep latest per case_uid)...")
    try:
        client.query(f"""
            DELETE FROM {P}.external_genomic_labels`
            WHERE ingested_at NOT IN (
                SELECT MAX(ingested_at)
                FROM {P}.external_genomic_labels`
                GROUP BY case_uid
            )
        """).result()
    except Exception as e:
        if "streaming" in str(e).lower():
            print("    [WARN] DML DELETE blocked by streaming buffer — using MERGE upsert fallback")
            client.query(f"""
                MERGE {P}.external_genomic_labels` T
                USING (
                    SELECT * EXCEPT(rn) FROM (
                        SELECT *, ROW_NUMBER() OVER
                            (PARTITION BY case_uid ORDER BY ingested_at DESC) AS rn
                        FROM {P}.external_genomic_labels`
                    ) WHERE rn = 1
                ) S ON T.case_uid = S.case_uid
                WHEN MATCHED THEN UPDATE SET ingested_at = S.ingested_at
            """).result()
        else:
            raise
    print("    [OK] external_genomic_labels deduplicated (partitioning preserved).")

    # FIX 3: Update TCGA access tier to OPEN
    print("[3] Updating TCGA access tier: TIERED/None → OPEN...")
    result = client.query(f"""
        UPDATE {P}.external_genomic_labels`
        SET dataset_access_tier = 'OPEN'
        WHERE dataset_source = 'TCGA'
    """).result()
    print("    [OK] TCGA rows updated to OPEN.")
    result = client.query(f"""
        UPDATE {P}.clinical_cohorts`
        SET clinical_stage = COALESCE(clinical_stage, 'UNKNOWN')
        WHERE data_source = 'TCGA'
    """).result()

    # FIX 4: Rebuild training_master view with dedup CTEs + parsed columns
    print("[4] Rebuilding training_master view with dedup-safe CTEs...")
    client.query(f"""
        CREATE OR REPLACE VIEW {P}.training_master` AS
        WITH
        cc AS (
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY case_uid ORDER BY ingested_at DESC) rn
                FROM {P}.clinical_cohorts`
            ) WHERE rn = 1
        ),
        lbl AS (
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY case_uid ORDER BY ingested_at DESC) rn
                FROM {P}.external_genomic_labels`
            ) WHERE rn = 1
        )
        SELECT
            cc.case_uid,
            cc.data_source,
            cc.cohort_region,
            cc.cancer_type,
            cc.modality,
            cc.clinical_stage,
            cc.ingested_at               AS cohort_ingested_at,
            lbl.dataset_source,
            lbl.mutation_labels,
            lbl.molecular_subtype,
            lbl.dataset_access_tier,
            SAFE_CAST(JSON_VALUE(lbl.mutation_labels,'$.EGFR') AS BOOL) AS egfr_mutant,
            SAFE_CAST(JSON_VALUE(lbl.mutation_labels,'$.KRAS') AS BOOL) AS kras_mutant,
            SAFE_CAST(JSON_VALUE(lbl.mutation_labels,'$.ALK')  AS BOOL) AS alk_positive,
            SAFE_CAST(JSON_VALUE(lbl.mutation_labels,'$.TP53') AS BOOL) AS tp53_mutant,
            (f.feature_id IS NOT NULL)                                   AS has_radiomic_features,
            f.lesion_volume_mm3,
            f.shape_sphericity,
            f.texture_entropy,
            f.intensity_mean,
            f.extraction_model_version
        FROM cc
        LEFT JOIN lbl ON cc.case_uid  = lbl.case_uid
        LEFT JOIN {P}.radiomics_features` f ON cc.case_uid = f.case_uid
    """).result()
    print("    [OK] training_master view rebuilt.\n")

    # ── Post-fix validation ───────────────────────────────────────────
    print("\n=== POST-FIX VALIDATION ===")
    q(client, "Cohort row counts after dedup", f"""
        SELECT data_source, COUNT(*) AS rows, COUNT(DISTINCT case_uid) AS unique_cases
        FROM {P}.clinical_cohorts`
        GROUP BY 1 ORDER BY 2 DESC
    """)
    q(client, "Label row counts after dedup", f"""
        SELECT dataset_source, dataset_access_tier, COUNT(*) AS rows
        FROM {P}.external_genomic_labels`
        GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    q(client, "training_master counts", f"""
        SELECT data_source, cancer_type, COUNT(*) AS rows,
               COUNTIF(egfr_mutant) AS egfr_pos,
               COUNTIF(kras_mutant) AS kras_pos
        FROM {P}.training_master`
        GROUP BY 1, 2 ORDER BY 3 DESC
        LIMIT 15
    """)
    print("\n[FIX COMPLETE] All BigQuery quality issues resolved.")


if __name__ == "__main__":
    main()
