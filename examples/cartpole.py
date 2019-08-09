import atexit
import datetime
import numpy as np
import os
import torch
import tensorboardX

from prob_mbrl import utils, models, algorithms, losses, envs
torch.set_num_threads(4)

if __name__ == '__main__':
    # parameters
    n_rnd = 4
    pred_H = 15
    control_H = 60
    N_particles = 100
    N_polopt = 1000
    N_dynopt = 2000
    dyn_components = 1
    dyn_hidden = [200] * 2
    pol_hidden = [200] * 2
    use_cuda = False
    learn_reward = True

    # initialize environment
    env = envs.mj_cartpole.Cartpole()
    results_filename = os.path.expanduser(
        "~/.prob_mbrl/results_%s_%s.pth.tar" %
        (env.__class__.__name__,
         datetime.datetime.now().strftime("%Y%m%d%H%M%S.%f")))
    D = env.observation_space.shape[0]
    U = env.action_space.shape[0]
    maxU = env.action_space.high

    # initialize reward/cost function
    if learn_reward or env.reward_func is None:
        reward_func = None
    else:
        reward_func = env.reward_func

    # initialize dynamics model
    dynE = 2 * (D + 1) if learn_reward else 2 * D
    if dyn_components > 1:
        output_density = models.MixtureDensity(dynE / 2, dyn_components)
        dynE = (dynE + 1) * dyn_components + 1
        log_likelihood_loss = losses.gaussian_mixture_log_likelihood
    else:
        output_density = models.DiagGaussianDensity(dynE / 2)
        log_likelihood_loss = losses.gaussian_log_likelihood

    dyn_model = models.mlp(D + U,
                           dynE,
                           dyn_hidden,
                           dropout_layers=[
                               models.modules.CDropout(0.1, 0.1)
                               for i in range(len(dyn_hidden))
                           ],
                           nonlin=torch.nn.ReLU)
    dyn = models.DynamicsModel(dyn_model,
                               reward_func=reward_func,
                               output_density=output_density).float()

    # initalize policy
    pol_model = models.mlp(D,
                           U,
                           pol_hidden,
                           dropout_layers=[
                               models.modules.BDropout(0.1)
                               for i in range(len(pol_hidden))
                           ],
                           nonlin=torch.nn.ReLU,
                           output_nonlin=torch.nn.Tanh)

    pol = models.Policy(pol_model, maxU).float()

    # initalize experience dataset
    exp = utils.ExperienceDataset()

    # initialize dynamics optimizer
    opt1 = torch.optim.Adam(dyn.parameters(), 1e-4)

    # initialize policy optimizer
    opt2 = torch.optim.Adam(pol.parameters(), 1e-4)

    if use_cuda and torch.cuda.is_available():
        dyn = dyn.cuda()
        pol = pol.cuda()

    writer = tensorboardX.SummaryWriter()

    # callbacks
    def on_close():
        writer.close()

    atexit.register(on_close)

    # policy learning loop
    for it in range(100 + n_rnd):
        if it < n_rnd:
            pol_ = lambda x, t: maxU * (2 * np.random.rand(U, ) - 1
                                        )  # noqa: E731
        else:
            pol_ = pol

        # apply policy
        ret = utils.apply_controller(
            env,
            pol_,
            control_H,
            callback=lambda *args, **kwargs: env.render())
        params_ = [] if it < n_rnd else [
            p.clone() for p in list(pol.parameters())
        ]
        exp.append_episode(*ret, policy_params=params_)
        exp.save(results_filename)

        if it < n_rnd - 1:
            continue
        ps_it = it - n_rnd + 1

        def on_iteration(i, loss, states, actions, rewards, opt, policy,
                         dynamics):
            writer.add_scalar('mc_pilco/episode_%d/training loss' % ps_it,
                              loss, i)
            if i % 100 == 0:
                #states = states.transpose(0, 1).cpu().detach().numpy()
                #actions = actions.transpose(0, 1).cpu().detach().numpy()
                #rewards = rewards.transpose(0, 1).cpu().detach().numpy()
                #utils.plot_trajectories(states,
                #                        actions,
                #                        rewards,
                #                        plot_samples=True)
                writer.flush()

        # train dynamics
        X, Y = exp.get_dynmodel_dataset(deltas=True, return_costs=learn_reward)
        dyn.set_dataset(X.to(dyn.X.device).float(), Y.to(dyn.X.device).float())
        utils.train_regressor(dyn,
                              N_dynopt,
                              N_particles,
                              True,
                              opt1,
                              log_likelihood=log_likelihood_loss,
                              summary_writer=writer,
                              summary_scope='model_learning/episode_%d' %
                              ps_it)

        # sample initial states for policy optimization
        x0 = exp.sample_states(N_particles,
                               timestep=0).to(dyn.X.device).float()
        x0 = x0 + 1e-1 * x0.std(0) * torch.randn_like(x0)
        x0 = x0.detach()
        utils.plot_rollout(x0, dyn, pol, control_H)

        # train policy
        print("Policy search iteration %d" % (ps_it + 1))
        algorithms.mc_pilco(x0,
                            dyn,
                            pol,
                            pred_H,
                            opt2,
                            exp,
                            N_polopt,
                            pegasus=True,
                            mm_states=False,
                            mm_rewards=False,
                            maximize=True,
                            clip_grad=1.0,
                            on_iteration=on_iteration)
        utils.plot_rollout(x0, dyn, pol, control_H)
        writer.add_scalar('robot/evaluation_loss',
                          torch.tensor(ret[2]).sum(), ps_it + 1)
