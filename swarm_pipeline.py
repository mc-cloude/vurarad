import kfp
from kfp import dsl
from kfp.dsl import component, Output, Artifact, Model, Dataset
from google.cloud import aiplatform

# Vura-Core v5.0 Swarm Optimizer Pipeline
# Automates the extraction of feedback and retraining of consensus weights

PROJECT_ID = "vurarad"
REGION = "me-central2"
IMAGE_NAME = "gcr.io/vurarad/vura-swarm-optimizer:v5"
BUCKET_NAME = "vurarad-swarm-configs"

@component(
    base_image="python:3.9-slim",
    packages_to_install=["google-cloud-bigquery", "pandas", "db-dtypes", "google-cloud-storage"]
)
def swarm_training_op(
    project_id: str,
    bucket_name: str,
    output_weights: Output[Artifact]
):
    import os
    import pandas as pd
    from google.cloud import bigquery
    from google.cloud import storage
    import json
    
    # Injected Logic from swarm_optimizer.py (for self-containment in component)
    bq_client = bigquery.Client(project=project_id)
    query = f"""
        SELECT ai_model_used, radiologist_agreement, ai_confidence, metadata
        FROM `{project_id}.vura_core.scan_events`
        WHERE event_type = 'AI_ANALYZED' AND radiologist_agreement IS NOT NULL
        LIMIT 1000
    """
    df = bq_client.query(query).to_dataframe()
    
    if df.empty:
        # Mock for pipeline demo if no real data
        df = pd.DataFrame([
            {'ai_model_used': 'sentinel', 'radiologist_agreement': True, 'ai_confidence': 0.9, 'metadata': '{"sentinel_alert": true}'},
            {'ai_model_used': 'neuro', 'radiologist_agreement': True, 'ai_confidence': 0.95}
        ])

    def calculate_reward(row):
        base = 1.0 if row['radiologist_agreement'] else -2.0
        reward = base * row['ai_confidence']
        # Metadata parsing
        try:
            meta = json.loads(row['metadata']) if isinstance(row['metadata'], str) else {}
            if meta.get('sentinel_alert') and row['radiologist_agreement']: reward += 0.5
        except: pass
        return reward

    df['reward'] = df.apply(calculate_reward, axis=1)
    weights = df.groupby('ai_model_used')['reward'].mean().to_dict()
    
    # Save & Upload
    weights_file = "swarm_weights.json"
    with open(weights_file, "w") as f:
        json.dump(weights, f)
        
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(f"v5/consensus/{weights_file}")
    blob.upload_from_filename(weights_file)
    
    output_weights.path = f"gs://{bucket_name}/v5/consensus/{weights_file}"

@dsl.pipeline(
    name="vura-swarm-convergence-pipeline",
    description="Vura-Core v5.0: Swarm Consensus Optimization"
)
def swarm_pipeline(project_id: str = PROJECT_ID, bucket_name: str = BUCKET_NAME):
    training_task = swarm_training_op(
        project_id=project_id,
        bucket_name=bucket_name
    )

if __name__ == "__main__":
    # Compile the pipeline
    from kfp import compiler
    compiler.Compiler().compile(
        pipeline_func=swarm_pipeline,
        package_path="swarm_pipeline.json"
    )
    print("✓ Pipeline Compiled: swarm_pipeline.json")
    
    # Optionally submit to Vertex AI
    # aiplatform.init(project=PROJECT_ID, location=REGION)
    # job = aiplatform.PipelineJob(
    #     display_name="vura-swarm-v5-run",
    #     template_path="swarm_pipeline.json",
    #     pipeline_root=f"gs://{BUCKET_NAME}/pipeline_root"
    # )
    # job.run()
