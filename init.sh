# cd home/vdemers/github/vllm/
# pyenv activate vllm-expt
# nvidia-smi
# CUDA_VISIBLE_DEVICES=7 vllm serve /data/chatterina/models/granite-8b-instruct-preview-4k-r240917a --dtype auto --port 8000 --served-model-name granite-8b-instruct-preview-4k-r240917a

# OR 

# CUDA_VISIBLE_DEVICES=7 vllm serve /data/chatterina/models/granite-8b-instruct-preview-4k-r240917a --dtype auto --port 8000 --served-model-name granite-8b-instruct-preview-4k-r240917a --controller http://0.0.0.0:21002 --worker-address http://0.0.0.0:8000