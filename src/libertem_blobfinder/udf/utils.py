import numpy as np
import matplotlib.pyplot as plt

import libertem_blobfinder.common.gridmatching as grm


def visualize_frame(ctx, ds, result, indices, r, y, x, axes, colors=None, stretch=10):
    '''
    Visualize the refinement of a specific frame in matplotlib axes
    '''
    # Get the frame from the dataset
    get_sample_frame = ctx.create_pick_analysis(dataset=ds, y=y, x=x)
    sample_frame = ctx.run(get_sample_frame)

    if y is None:
        select = (x, )
    else:
        select = (y, x)

    d = sample_frame[0].raw_data.astype(np.float32)

    pcm = axes.imshow(np.log(d - np.min(d) + 1))

    refined = result['refineds'].data[select]
    elevations = result['peak_elevations'].data[select]
    selector = result['selector'].data[select]

    max_elevation = np.max(elevations)

    # Calclate the best fit positions to compare with the
    # individual peak positions.
    # A difference between best fit and individual peaks highlights outliers.
    calculated = grm.calc_coords(
        zero=result['zero'].data[select],
        a=result['a'].data[select],
        b=result['b'].data[select],
        indices=indices
    )

    paint_markers(
        axes=axes,
        r=r,
        refined=refined,
        normalized_elevations=elevations/max_elevation,
        calculated=calculated,
        selector=selector,
        zero=result['zero'].data[select],
        a=result['a'].data[select],
        b=result['b'].data[select],
        colors=colors,
        stretch=stretch,
    )
    return pcm


def paint_markers(axes, r, refined, normalized_elevations, calculated, selector, zero, a, b,
        colors=None, stretch=10):
    if colors is None:
        colors = {
            'marker': 'w',
            'arrow': 'r',
            'missing': 'r',
            'a': 'b',
            'b': 'g',
        }

    axes.arrow(*np.flip(zero), *(np.flip(a)), color=colors['a'])
    axes.arrow(*np.flip(zero), *(np.flip(b)), color=colors['b'])

    # Plot markers for the individual peak positions.
    # The alpha channel represents the peak elevation, which is used as a weight in the fit.
    for i in range(len(refined)):
        p = np.flip(refined[i])
        a = max(0, normalized_elevations[i])
        p0 = np.flip(calculated[i])
        if selector[i]:
            axes.add_artist(plt.Circle(p, r, color=colors['marker'], fill=False, alpha=a))
            axes.add_artist(plt.Circle(p0, 1, color=colors['arrow'], fill=True, alpha=a))
            axes.arrow(*p0, *(p-p0)*stretch, color=colors['arrow'], alpha=a)
        else:
            (yy, xx) = calculated[i]
            xy = (xx - r, yy - r)
            axes.add_artist(plt.Rectangle(xy, 2*r, 2*r, color=colors['missing'], fill=False))
