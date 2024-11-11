# If you don't have us.icr.io/aims/custom_vllm:base on the machine,
# make sure you run `sh build.sh` prior to running this script

# Main difference bewteen :base and :dev/:latest ?
#    :base build the fill vllm image
#    :dev/latest builds FROM :base and adds the wanted special tokens we want to escape

docker build -f Dockerfile.special_tokens -t us.icr.io/aims/custom_vllm:dev .