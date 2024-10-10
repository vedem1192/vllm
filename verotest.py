# VLLM RUN COMMAND
# SPECIAL_TOKENS="<|TOKEN|>" CUDA_VISIBLE_DEVICES=1 vllm serve /data/chatterina/models/granite-8b-instruct-preview-4k-r240917a --dtype auto --port 8001

# THE MAGIC HAPPENS HERE :
# :: HERE WE HACK THE WORLD


# FASTAPI PAYLOAD
"""
{
  "model": "/data/chatterina/models/granite-8b-instruct-preview-4k-r240917a",
  "prompt": [
    "<|start_of_role|>available_tools<|end_of_role|>\n{\n    \"type\": \"function\",\n    \"function\": {\n        \"name\": \"create_github_issue\",\n        \"description\": \"Create a github issue object\",\n        \"parameters\": {\n            \"type\": \"object\",\n            \"properties\": {\n                \"title\": {\n                    \"type\": \"string\",\n                    \"description\": \"Title of the issue\"\n                },\n                \"content\": {\n                    \"type\": \"string\",\n                    \"description\": \"Descriprion of the issue\"\n                },\n                \"author\": {\n                    \"type\": \"string\",\n                    \"nullable\": true,\n                    \"description\": \"The author of the issue. Defaults to None.\"\n                },\n                \"assignee\": {\n                    \"type\": \"string\",\n                    \"nullable\": true,\n                    \"description\": \"The assigned developer to the issue. Defaults to None.\"\n                }\n            },\n            \"required\": [\n                \"title\",\n                \"content\"\n            ]\n        }\n    }\n}<|end_of_text|>\n<|start_of_role|>user<|end_of_role|>Create a github issues to add a new language to the site. The new language should be French.<|end_of_text|>\n<|start_of_role|>assistant<|end_of_role|>"
  ],
  "echo": false,
  "max_tokens": 1000,
  "temperature": 0,
  "top_p": 1,
  "top_k": 50,
  "repetition_penalty": 1.2,
  "include_stop_str_in_output": false,
  "min_tokens": 1,
  "skip_special_tokens": true,
  "spaces_between_special_tokens": true
}
"""


# IMAGE
# docker build -f Dockerfile -t us.icr.io/aims/vdvllm:current .
# docker build -f Dockerfile -t us.icr.io/aims/vdvllm:dev .



# TO RUN IN DOCKER CONTAINER
# docker run \
# --runtime nvidia \
# --ipc host \
# --gpus device=1 \
# --gpu-memory-utilization min(0.9,  num_params * 4/80)
# --name vllm-model-granite-8b-instruct-preview-4k-r240917a \
# -v ~/.cache/huggingface:/root/.cache/huggingface \
# -v /data/chatterina/models:/data/chatterina/models \
# -p 8000:8000 \ 
# -d \
# us.icr.io/aims/vdvllm:dev --model /data/chatterina/models/granite-8b-instruct-preview-4k-r240917a

# RUN AS ONE LINER (ABOVE HAS SOME FORMATTING ISSUES)
# docker run --runtime nvidia --ipc host --gpus device=1 --name vllm-model-granite-8b-instruct-preview-4k-r240917a -v ~/.cache/huggingface:/root/.cache/huggingface -v /data/chatterina/models:/data/chatterina/models -p 8000:8000 -d us.icr.io/aims/vdvllm:dev --model /data/chatterina/models/granite-8b-instruct-preview-4k-r240917a

