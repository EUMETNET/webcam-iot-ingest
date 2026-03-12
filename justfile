# list recipes
default:
    @just --list

set positional-arguments

# Build and run the default docker services
up: build services

# Build and run the default docker services and start up monitoring
local: up monitoring

# ---------------------------------------------------------------------------- #
#                                    build                                     #
# ---------------------------------------------------------------------------- #

# Build the docker images
build:
    docker compose --env-file .env --profile monitoring build --no-cache


# # ---------------------------------------------------------------------------- #
# #                                     local                                    #
# # ---------------------------------------------------------------------------- #

# Start the default docker compose containers
services:
    docker compose --env-file .env up -d

# Start the monitoring containers
monitoring:
    docker compose --env-file .env --profile monitoring up -d

# Stop all containers and remove their volumes
destroy:
    docker compose --profile monitoring down --volumes
