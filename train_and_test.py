import time
import torch
from sklearn.metrics import roc_auc_score
import numpy as np
import pandas as pd
import csv
from helpers import list_of_distances, make_one_hot
# pylint: disable=no-member

def _train_or_test(model, dataloader, optimizer=None, class_specific=True, use_l1_mask=True,
                   coefs=None, log=print, save_logits=False, finer_loader=None):
    '''
    model: the multi-gpu model
    dataloader:
    optimizer: if None, will be test evaluation
    '''
    is_train = optimizer is not None 
    start = time.time()
    n_examples = 0
    n_correct = 0
    n_batches = 0
    total_output = []
    total_one_hot_label = []
    confusion_matrix = [0,0,0,0]
    total_cross_entropy = 0
    total_cluster_cost = 0
    # separation cost is meaningful only for class_specific
    total_separation_cost = 0
    total_avg_separation_cost = 0
    total_fa_cost = 0
    with_fa = False # intialization, see line 41

    for i, (image, label, patient_id) in enumerate(dataloader):
        # get one batch from finer datatloader
        if finer_loader:
            finer_image, finer_label, _ = next(iter(finer_loader))
            # print(image.shape)
            image = torch.cat((image, finer_image))
            label = torch.cat((label, finer_label))
            # print(image.shape)
        if image.shape[1] == 4:
            with_fa = True
            fine_annotation = image[:, 3:4, :, :]
            image = image[:, 0:3, :, :]  #(no view, create slice)
        elif image.shape[1] == 3:
            fine_annotation = torch.zeros(size=(image.shape[0], 1, image.shape[2], image.shape[3])) #means everything can be relevant
            image = image
        fine_annotation = fine_annotation.cuda()
        input = image.cuda()
        target = label.cuda()

        # torch.enable_grad() has no effect outside of no_grad()
        grad_req = torch.enable_grad() if is_train else torch.no_grad()
        with grad_req:
            # nn.Module has implemented __call__() function
            # so no need to call .forward
            output, min_distances, upsampled_activation = model(input)
            # compute loss
            cross_entropy = torch.nn.functional.cross_entropy(output, target)

            # only save to csv on test
            if not is_train and save_logits:
                _output_scores = [",".join([str(score) for score in scores.cpu().numpy()]) for scores in output]
                write_file = './logit_csvs/0218_training_3_class_margin_logits.csv'
                with open(write_file, mode='a') as logit_file:
                    logit_writer = csv.writer(logit_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                    for _index in range(len(patient_id)):
                        logit_writer.writerow([patient_id[_index], _output_scores[_index]])
                log(f'Wrote to {write_file}.')

            if class_specific:
                max_dist = (model.module.prototype_shape[1]
                            * model.module.prototype_shape[2]
                            * model.module.prototype_shape[3])

                # prototypes_of_correct_class is a tensor of shape batch_size * num_prototypes
                # calculate cluster cost
                prototypes_of_correct_class = torch.t(model.module.prototype_class_identity[:,label]).cuda()
                inverted_distances, _ = torch.max((max_dist - min_distances) * prototypes_of_correct_class, dim=1)
                cluster_cost = torch.mean(max_dist - inverted_distances)
                # print("before change")

                # calculate separation cost
                prototypes_of_wrong_class = 1 - prototypes_of_correct_class
                inverted_distances_to_nontarget_prototypes, _ = \
                    torch.max((max_dist - min_distances) * prototypes_of_wrong_class, dim=1)
                separation_cost = torch.mean(max_dist - inverted_distances_to_nontarget_prototypes)
                # print("after change")

                # calculate avg cluster cost
                avg_separation_cost = \
                    torch.sum(min_distances * prototypes_of_wrong_class, dim=1) / torch.sum(prototypes_of_wrong_class, dim=1)
                avg_separation_cost = torch.mean(avg_separation_cost)
                
                if use_l1_mask:
                    l1_mask = 1 - torch.t(model.module.prototype_class_identity).cuda()
                    l1 = (model.module.last_layer.weight * l1_mask).norm(p=1)
                else:
                    l1 = model.module.last_layer.weight.norm(p=1) 

                #fine annotation loss
                fine_annotation_cost = 0
                if with_fa:
                    proto_num_per_class = model.module.num_prototypes // model.module.num_classes
                    all_white_mask = torch.ones(image.shape[2], image.shape[3]).cuda()
                    for index in range(image.shape[0]):
                        fine_annotation_cost += torch.norm(upsampled_activation[index, :label[index] * proto_num_per_class] * (1 * all_white_mask)) + \
                            torch.norm(upsampled_activation[index, label[index] * proto_num_per_class : (label[index] + 1) * proto_num_per_class] * (1 * fine_annotation[index])) + \
                                torch.norm(upsampled_activation[index, (label[index]+1) * proto_num_per_class:] * (1 * all_white_mask))
                

            else:
                min_distance, _ = torch.min(min_distances, dim=1)
                # label=0 negative, label=1 positive, minimize cluster loss maximize separation loss
                # all prototypes are positive
                positive_sample_index = torch.flatten(torch.nonzero(label)).tolist()
                negative_sample_index = torch.flatten(torch.nonzero(label == 0)).tolist()
                if len(positive_sample_index) > 0:
                    positive_proto_distance = min_distance[positive_sample_index]
                else:
                    positive_proto_distance = torch.zeros(1)

                if len(negative_sample_index) > 0:
                    negative_proto_distance = min_distance[negative_sample_index]
                else:
                    negative_proto_distance = torch.zeros(1)

                cluster_cost = torch.mean(positive_proto_distance)
                separation_cost = torch.mean(negative_proto_distance)
                l1 = model.module.last_layer.weight.norm(p=1)

            # evaluation statistics
            _, predicted = torch.max(output.data, 1)
            n_examples += target.size(0)
            n_correct += (predicted == target).sum().item()

            # confusion matrix
            for t_idx, t in enumerate(target):
                if predicted[t_idx] == t and predicted[t_idx] == 1:  # true positive
                    confusion_matrix[0] += 1
                elif t == 0 and predicted[t_idx] == 1:
                    confusion_matrix[1] += 1  # false positives
                elif t == 1 and predicted[t_idx] == 0:
                    confusion_matrix[2] += 1  # false negative
                else:
                    confusion_matrix[3] += 1

            # one hot label for AUC
            one_hot_label = np.zeros(shape=(len(target), model.module.num_classes))
            for k in range(len(target)):
                one_hot_label[k][target[k].item()] = 1

            prob = torch.nn.functional.softmax(output, dim=1)
            total_output.extend(prob.data.cpu().numpy())
            total_one_hot_label.extend(one_hot_label)
            # one hot label for AUC

            n_batches += 1
            total_cross_entropy += cross_entropy.item()
            total_cluster_cost += cluster_cost.item()
            total_separation_cost += separation_cost.item()
            total_fa_cost += fine_annotation_cost
            if class_specific:
                total_avg_separation_cost += avg_separation_cost.item()

        # compute gradient and do SGD step
        if is_train:
            if coefs is not None:
                loss = (coefs['crs_ent'] * cross_entropy
                      + coefs['clst'] * cluster_cost
                      + coefs['sep'] * separation_cost
                      + coefs['l1'] * l1
                      + coefs['fine'] * fine_annotation_cost)
            else:
                loss = cross_entropy + 0.8 * cluster_cost - 0.08 * separation_cost + 1e-4 * l1

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        del input
        del target
        del output
        del predicted
        del min_distances

    end = time.time()

    log('\ttime: \t{0}'.format(end -  start))
    log('\tcross ent: \t{0}'.format(total_cross_entropy / n_batches))
    log('\tcluster: \t{0}'.format(total_cluster_cost / n_batches))
    log('\tseparation:\t{0}'.format(total_separation_cost / n_batches))
    log('\tfine annotation:\t{0}'.format(total_fa_cost / n_batches))
    if class_specific:
        log('\tavg separation:\t{0}'.format(total_avg_separation_cost / n_batches))

    avg_auc = 0
    for auc_idx in range(len(total_one_hot_label[0])):
        avg_auc += roc_auc_score(np.array(total_one_hot_label)[:, auc_idx], np.array(total_output)[:, auc_idx]) / len(total_one_hot_label[0])
        log("\tauc score for class {} is: \t\t{}".format(auc_idx,
                                                         roc_auc_score(np.array(total_one_hot_label)[:, auc_idx], np.array(total_output)[:, auc_idx])))

    log('\taccu: \t\t{0}%'.format(n_correct / n_examples * 100))
    log('\tl1: \t\t{0}'.format(model.module.last_layer.weight.norm(p=1).item()))
    p = model.module.prototype_vectors.view(model.module.num_prototypes, -1).cpu()
    with torch.no_grad():
        p_avg_pair_dist = torch.mean(list_of_distances(p, p))
    log('\tp dist pair: \t{0}'.format(p_avg_pair_dist.item()))
    log('\tthe confusion matrix is: \t\t{0}'.format(confusion_matrix))

    return avg_auc


def train(model, dataloader, optimizer, class_specific=False, coefs=None, log=print, finer_loader=None):
    assert(optimizer is not None)
    
    log('\ttrain')
    model.train()
    return _train_or_test(model=model, dataloader=dataloader, optimizer=optimizer,
                          class_specific=class_specific, coefs=coefs, log=log, finer_loader=finer_loader)


def test(model, dataloader, class_specific=False, log=print, save_logits=False):
    log('\ttest')
    model.eval()
    return _train_or_test(model=model, dataloader=dataloader, optimizer=None,
                          class_specific=class_specific, log=log, save_logits=save_logits)


def last_only(model, log=print):
    for p in model.module.features.parameters():
        p.requires_grad = False
    for p in model.module.add_on_layers.parameters():
        p.requires_grad = False
    model.module.prototype_vectors.requires_grad = False
    for p in model.module.last_layer.parameters():
        p.requires_grad = True
    
    log('\tlast layer')


def warm_only(model, log=print):
    for p in model.module.features.parameters():
        p.requires_grad = False
    for p in model.module.add_on_layers.parameters():
        p.requires_grad = True
    model.module.prototype_vectors.requires_grad = True
    for p in model.module.last_layer.parameters():
        p.requires_grad = True
    
    log('\twarm')


def joint(model, log=print):
    for p in model.module.features.parameters():
        p.requires_grad = True
    for p in model.module.add_on_layers.parameters():
        p.requires_grad = True
    model.module.prototype_vectors.requires_grad = True
    for p in model.module.last_layer.parameters():
        p.requires_grad = True
    
    log('\tjoint')
