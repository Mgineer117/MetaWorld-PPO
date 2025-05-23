import torch


def estimate_advantages(
    rewards, terminals, values, gamma=0.99, gae=0.95, device=torch.device("cpu")
):
    rewards, terminals, values = (
        rewards.to(torch.device("cpu")),
        terminals.to(torch.device("cpu")),
        values.to(torch.device("cpu")),
    )
    tensor_type = type(rewards)
    deltas = tensor_type(rewards.size(0), 1)
    advantages = tensor_type(rewards.size(0), 1)

    prev_value = 0
    prev_advantage = 0
    for i in reversed(range(rewards.size(0))):
        deltas[i] = rewards[i] + gamma * prev_value * (1 - terminals[i]) - values[i]
        advantages[i] = deltas[i] + gamma * gae * prev_advantage * (1 - terminals[i])

        prev_value = values[i, 0]
        prev_advantage = advantages[i, 0]

    returns = values + advantages
    # advantages = (advantages - advantages.mean()) / advantages.std()
    advantages, returns = advantages.to(device), returns.to(device)
    return advantages, returns


def get_policy(env, args):
    algo_name = args.algo_name
    nupdates = args.timesteps / (args.minibatch_size * args.num_minibatch)

    if algo_name == "ppo":
        from policy.ppo import PPO
        from policy.layers.ppo_networks import PPO_Actor, PPO_Critic

        actor = PPO_Actor(
            args.state_dim,
            hidden_dim=args.actor_dim,
            a_dim=args.action_dim,
        )

        critic = PPO_Critic(args.state_dim, hidden_dim=args.critic_dim)

        policy = PPO(
            actor=actor,
            critic=critic,
            actor_lr=args.actor_lr,
            critic_lr=args.critic_lr,
            num_minibatch=args.num_minibatch,
            minibatch_size=args.minibatch_size,
            eps_clip=args.eps_clip,
            entropy_scaler=args.entropy_scaler,
            target_kl=args.target_kl,
            gamma=args.gamma,
            gae=args.gae,
            K=args.K_epochs,
            nupdates=nupdates,
            device=args.device,
        )

    return policy
